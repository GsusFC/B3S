from __future__ import annotations

import inspect
import json
from copy import deepcopy
from urllib.error import HTTPError

import pytest


# fmt: off
class _Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body, self.status, self.limit = body, status, 0

    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def getcode(self): return self.status
    def read(self, limit): self.limit = limit; return self.body
def _request():
    from tests.test_evidence_vault_sv9_judgment_shadow import _Provider, _Repository, _run
    provider = _Provider(); _run(_Repository(), provider)
    return provider.calls[0]["request"]
def _payload(request):
    rows = [{"tile_id": row["tile_id"], "assessment_state": "ok", "supporting_evidence": [{key: row["evidence"][0][key] for key in ("evidence_ref", "evidence_fingerprint")}]} for row in request["requested_tiles"]]
    return {"component_key": request["component_key"], "series_fingerprint": request["current_series_fingerprint"], "request_fingerprint": request["canonical_request_fingerprint"], "status": "evaluated", "tile_results": rows}
def _body(payload, message=None, envelope=None, **choice):
    return json.dumps({**(envelope or {}), "choices": [{"message": {"content": json.dumps(payload), **(message or {})}, **choice}]}).encode()
def test_closed_outcome_and_adapter_have_no_generic_transport_dependency():
    from src.sv9.incremental_flow_adapter import FlowSv9StrictComponentAdapter
    from src.sv9.shadow_component_provider import ShadowProviderOutcome
    class _Dict(dict): pass
    payload = _payload(_request())
    for value, reason in ((None, None), ({"raw": "marker"}, None), (payload, "timeout"), (_Dict(payload), None), (None, "unknown")):
        outcome = ShadowProviderOutcome(value, reason)
        assert outcome.payload is None and outcome.reason_code == "response_invalid" and "marker" not in repr(outcome)
    stable = ShadowProviderOutcome.success(payload); payload["tile_results"][0]["assessment_state"] = "no"; assert stable.payload["tile_results"][0]["assessment_state"] == "ok"
    assert "_call_json" not in inspect.getsource(FlowSv9StrictComponentAdapter)
def test_provider_builds_one_strict_request_and_has_no_stale_state(monkeypatch):
    from src.sv9.shadow_component_provider import COMPONENT_EVALUATION_SCHEMA, FlowSv9ShadowJsonProvider, _MAX_RESPONSE
    request, calls = _request(), []; expected, second_request = _payload(request), deepcopy(request)
    first_response = _Response(_body(expected, finish_reason="stop")); responses = [first_response, _Response(_body(expected, finish_reason="stop"))]
    def open_(req, timeout):
        calls.append((req, timeout, req.data))
        if len(calls) == 1: monkeypatch.setitem(COMPONENT_EVALUATION_SCHEMA["properties"]["component_key"], "type", "number"); req.data = b"{}"
        return responses.pop(0)
    monkeypatch.setattr("src.sv9.shadow_component_provider._open_once", open_)
    provider = FlowSv9ShadowJsonProvider()
    first = provider.evaluate_component_json(request, environ={"BRAND3_LLM_API_KEY": "test-key"}, model="flow-model"); request["requested_tiles"][0]["evidence"][0]["evidence_ref"] = "mutated"
    second = provider.evaluate_component_json(second_request, environ={"BRAND3_LLM_API_KEY": "test-key"}, model="flow-model")
    body = json.loads(calls[0][2])
    assert provider.__slots__ == () and not hasattr(provider, "__dict__") and first.payload == expected
    assert second.payload == expected and len(calls) == 2 and calls[0][1] == 35
    assert body["model"] == "flow-model" and body["messages"][0]["content"] == "Evaluate only the requested SV9 tiles. Return no prose or extra fields." and json.loads(body["messages"][1]["content"])["requested_tiles"][0]["evidence"][0]["evidence_ref"] != "mutated"
    assert body["response_format"]["json_schema"]["strict"] is True and responses == [] and first_response.limit == _MAX_RESPONSE + 1 and json.loads(calls[1][0].data)["response_format"]["json_schema"]["schema"]["properties"]["component_key"]["type"] == "string"
def test_provider_rejects_invalid_input_before_configuration_or_transport(monkeypatch):
    from src.sv9.shadow_component_provider import FlowSv9ShadowJsonProvider
    import src.sv9.shadow_component_provider as provider_module
    config_calls, transport_calls = [], []
    monkeypatch.setattr(provider_module, "resolve_shadow_provider_config", lambda value: config_calls.append(value))
    monkeypatch.setattr(provider_module, "_open_once", lambda *_: transport_calls.append(1))
    invalid = deepcopy(_request()); invalid.pop("component_key")
    outcome = FlowSv9ShadowJsonProvider().evaluate_component_json(invalid, environ={"BRAND3_LLM_API_KEY": "test-key"}, model="flow-model")
    assert outcome.payload is None and outcome.reason_code == "request_invalid" and not config_calls and not transport_calls


def test_open_once_disables_ambient_proxy_and_redirects(monkeypatch):
    from urllib.request import ProxyHandler
    import src.sv9.shadow_component_provider as provider
    captured = []
    class _Opener:
        def open(self, *_args, **_kwargs): return None
    monkeypatch.setenv("HTTP_PROXY", "x"); monkeypatch.setenv("HTTPS_PROXY", "x")
    monkeypatch.setattr(provider, "build_opener", lambda *handlers: captured.extend(handlers) or _Opener())
    assert provider._open_once(object(), 1) is None
    assert [type(handler) for handler in captured] == [ProxyHandler, provider._NoRedirect] and captured[0].proxies == {}


def test_invalid_configuration_never_calls_transport(monkeypatch):
    from src.sv9.shadow_component_provider import FlowSv9ShadowJsonProvider
    calls = []; monkeypatch.setattr("src.sv9.shadow_component_provider._open_once", lambda *_: calls.append(1))
    outcome = FlowSv9ShadowJsonProvider().evaluate_component_json(_request(), environ={}, model="flow-model")
    assert outcome.payload is None and outcome.reason_code == "configuration_missing_api_key" and not calls


def test_actual_provider_matches_workset_and_replay(monkeypatch):
    from src.sv9.shadow_component_provider import FlowSv9ShadowJsonProvider
    from tests.test_evidence_vault_sv9_judgment_shadow import _Repository, _run
    calls = []
    def open_(outbound, _timeout):
        request = json.loads(json.loads(outbound.data)["messages"][1]["content"]); calls.append(1); return _Response(_body(_payload(request)))
    monkeypatch.setattr("src.sv9.shadow_component_provider._open_once", open_)
    repository, provider = _Repository(), FlowSv9ShadowJsonProvider(); options = {"environ": {"BRAND3_LLM_API_KEY": "test-key"}, "model": "flow-model"}
    assert _run(repository, provider, **options)["calls_issued"] == len(calls) == 10
    assert _run(repository, provider, **options)["calls_issued"] == 0 and len(calls) == 10
@pytest.mark.parametrize(("response", "reason"), ((lambda: _Response(b"", 302), "http_failure"), (lambda: (_ for _ in ()).throw(HTTPError("", 500, "error", {}, None)), "http_failure"), (lambda: (_ for _ in ()).throw(TimeoutError()), "timeout"), (lambda: (_ for _ in ()).throw(RuntimeError()), "transport_failure"), (lambda: _Response(b'{"choices":[],"choices":[]}'), "response_invalid"), (lambda: _Response(_body({"component_key": float("nan")})), "response_invalid"), (lambda: _Response(b'{"choices":[{"message":{"content":"{\\"component_key\\":\\"a\\",\\"component_key\\":\\"b\\"}"}}]}'), "response_invalid"), (lambda: _Response(b"\xff"), "response_invalid"), (lambda: _Response(b"x" * (512 * 1024 + 1)), "response_invalid"), (lambda: _Response(_body(_payload(_request()), envelope={"error": None})), "response_invalid"), (lambda: _Response(_body(_payload(_request()), error=None)), "response_invalid"), (lambda: _Response(_body(_payload(_request()), message={"error": None})), "response_invalid"), (lambda: _Response(_body(_payload(_request()), message={"refusal": None})), "response_invalid"), (lambda: _Response(_body(_payload(_request()), message={"tool_calls": []})), "response_invalid"), (lambda: _Response(_body(_payload(_request()), message={"function_call": {}})), "response_invalid"), (lambda: _Response(_body(_payload(_request()), finish_reason="length")), "response_invalid"), (lambda: _Response(_body(_payload(_request()), status="failed")), "response_invalid")))
def test_provider_failure_matrix_is_closed_and_single_shot(monkeypatch, response, reason, caplog):
    from src.sv9.shadow_component_provider import FlowSv9ShadowJsonProvider
    calls = []
    monkeypatch.setattr("src.sv9.shadow_component_provider._open_once", lambda *_: calls.append(1) or response())
    outcome = FlowSv9ShadowJsonProvider().evaluate_component_json(_request(), environ={"BRAND3_LLM_API_KEY": "test-key"}, model="flow-model")
    assert outcome.payload is None and outcome.reason_code == reason and len(calls) == 1 and "test-key" not in repr(outcome) + caplog.text
# fmt: on
