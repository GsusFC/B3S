"""Stateless, one-shot OpenAI-compatible transport for SV9 shadow judgments."""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from src.sv9 import incremental_evaluation as ie
from src.sv9 import judgment_memory as memory
from src.sv9.shadow_provider_config import resolve_shadow_provider_config

# fmt: off
_REASONS = frozenset("configuration_missing_api_key configuration_invalid_endpoint configuration_invalid_timeout request_invalid http_failure transport_failure timeout response_invalid".split())
_ENV_NAMES = ("BRAND3_LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "BRAND3_LLM_BASE_URL", "BRAND3_LLM_CALL_TIMEOUT_SECONDS")
_MAX_REQUEST = _MAX_RESPONSE = 512 * 1024
_SYSTEM_PROMPT = "Evaluate only the requested SV9 tiles. Return no prose or extra fields."
COMPONENT_EVALUATION_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": False, "required": ["component_key", "series_fingerprint", "request_fingerprint", "status", "tile_results"], "properties": {"component_key": {"type": "string"}, "series_fingerprint": {"type": "string"}, "request_fingerprint": {"type": "string"}, "status": {"enum": ["evaluated", "not_detected"]}, "tile_results": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["tile_id", "assessment_state", "supporting_evidence"], "properties": {"tile_id": {"type": "string"}, "assessment_state": {"enum": ["ok", "no", "sin_evidencia"]}, "supporting_evidence": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["evidence_ref", "evidence_fingerprint"], "properties": {"evidence_ref": {"type": "string"}, "evidence_fingerprint": {"type": "string"}}}}}}}}}
_SCHEMA = json.dumps(COMPONENT_EVALUATION_SCHEMA, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
@dataclass(frozen=True, slots=True, repr=False)
class ShadowProviderOutcome:
    payload: dict[str, Any] | None = None
    reason_code: Literal["configuration_missing_api_key", "configuration_invalid_endpoint", "configuration_invalid_timeout", "request_invalid", "http_failure", "transport_failure", "timeout", "response_invalid"] | None = None

    def __post_init__(self) -> None:
        payload = _valid_payload(self.payload)
        if self.reason_code is None and payload is not None: object.__setattr__(self, "payload", payload); return
        if self.payload is None and type(self.reason_code) is str and self.reason_code in _REASONS: return
        object.__setattr__(self, "payload", None); object.__setattr__(self, "reason_code", "response_invalid")

    def __repr__(self) -> str:
        return f"ShadowProviderOutcome(payload={'<redacted>' if self.payload is not None else None}, reason_code={self.reason_code!r})"

    @classmethod
    def success(cls, payload: dict[str, Any]) -> "ShadowProviderOutcome": return cls(payload=payload)

    @classmethod
    def failure(cls, reason: str) -> "ShadowProviderOutcome": return cls(reason_code=reason if type(reason) is str and reason in _REASONS else "response_invalid")
def shadow_provider_environment_snapshot(environ: Mapping[str, object] | object) -> Mapping[str, object]:
    values: dict[str, object] = {}
    try:
        for name in _ENV_NAMES:
            if name in environ: values[name] = environ[name]
    except Exception:
        values = {}
    return MappingProxyType(values)
def _valid_payload(value: object) -> dict[str, Any] | None:
    try:
        return ie._canon(value) if type(value) is dict and ie._evaluation(value, False) is not None else None
    except Exception:
        return None
def _upstream(rows: object, component: str) -> bool:
    if type(rows) is not list: return False
    if component != "coherencia": return rows == []
    index = 0
    for parent in ie._COMPONENTS[: ie._COMPONENTS.index(component)]:
        row = rows[index] if index < len(rows) else None
        if type(row) is dict and set(row) == {"component_key", "status", "canonical_component_sentinel_fingerprint"}:
            if row["component_key"] != parent or row["status"] != "not_detected" or ie._sha(row["canonical_component_sentinel_fingerprint"]) != row["canonical_component_sentinel_fingerprint"]: return False
            index += 1; continue
        for tile in ie._COMPONENT_TILES[parent]:
            row = rows[index] if index < len(rows) else None
            if type(row) is not dict or set(row) != {"tile_id", "assessment_state", "canonical_judgment_fingerprint"} or row["tile_id"] != tile or row["assessment_state"] not in {"ok", "no", "sin_evidencia"} or ie._sha(row["canonical_judgment_fingerprint"]) != row["canonical_judgment_fingerprint"]: return False
            index += 1
    return index == len(rows)
def request_binding(request: Mapping[str, Any]) -> tuple[str, list[str], dict[str, list[dict[str, str]]]] | None:
    try:
        if type(request) is not dict or set(request) != ie._REQUEST_FIELDS: raise ValueError
        ie._json(request); component = ie._text(request["component_key"])
        if component not in ie._COMPONENT_TILES or request["schema_version"] != ie.COMPONENT_REQUEST_VERSION: raise ValueError
        if any(ie._sha(request[key]) != request[key] for key in ("plan_fingerprint", "candidate_series_fingerprint", "current_series_fingerprint", "evidence_packet_fingerprint", "canonical_request_fingerprint")): raise ValueError
        series = memory.validate_judgment_series_contract(request["current_series_contract"])
        if memory.canonical_fingerprint("sv9-judgment-series-fingerprint-v1", series) != request["current_series_fingerprint"] or ie._origin(request["capture_origin"], "capture") != request["capture_origin"] or ie._origin(request["operation_origin"], "operation") != request["operation_origin"] or not _upstream(request["upstream_candidate_state"], component): raise ValueError
        unsigned = {key: request[key] for key in request if key != "canonical_request_fingerprint"}
        if memory.canonical_fingerprint(ie._REQUEST_FINGERPRINT, unsigned) != request["canonical_request_fingerprint"] or type(request["requested_tiles"]) is not list: raise ValueError
        tiles, evidence = [], {}
        for row in request["requested_tiles"]:
            if type(row) is not dict or set(row) != {"tile_id", "tile_key", "definition", "evidence"} or row["tile_id"] not in ie._COMPONENT_TILES[component]: raise ValueError
            tile, expected = ie._text(row["tile_id"]), ie._BY_TILE[row["tile_id"]]
            if row["tile_key"] != expected[2] or ie._canon(row["definition"]) != ie._canon(expected[3]): raise ValueError
            values = [{key: item[key] for key in ("evidence_ref", "evidence_fingerprint")} for item in ie._evidence(row["evidence"], True)]
            if tile in evidence: raise ValueError
            tiles.append(tile); evidence[tile] = values
        return (component, tiles, evidence) if tiles and tiles == sorted(tiles, key=ie._ORDER.__getitem__) else None
    except Exception:
        return None
class _NoRedirect(HTTPRedirectHandler):
    def http_error_302(self, request, response, code, message, headers): raise HTTPError(request.full_url, code, message, headers, response)
    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302
def _open_once(request: Request, timeout: int): return build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout)
def _reject(_value: str): raise ValueError
def _loads(value: str):
    def pairs(rows):
        result = {}
        for key, item in rows:
            if key in result: raise ValueError
            result[key] = item
        return result
    return json.loads(value, object_pairs_hook=pairs, parse_float=_reject, parse_constant=_reject)
def _model(value: object) -> str | None: return value if type(value) is str and 0 < len(value) <= 256 and value == value.strip() and all(char.isprintable() and not char.isspace() for char in value) else None
def _body(request: dict[str, Any], model: str) -> bytes:
    user = json.dumps(ie._canon(request), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not user or len(user) > _MAX_REQUEST: raise ValueError
    body = {"model": model, "messages": [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user.decode("utf-8")}], "max_tokens": 6000, "temperature": 0, "response_format": {"type": "json_schema", "json_schema": {"name": "sv9_strict_component_evaluation", "strict": True, "schema": json.loads(_SCHEMA)}}}
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > _MAX_REQUEST: raise ValueError
    return encoded
def _content(data: bytes) -> dict[str, Any] | None:
    try:
        envelope = _loads(data.decode("utf-8", "strict")); choices = envelope.get("choices") if type(envelope) is dict else None
        choice = choices[0] if type(choices) is list and len(choices) == 1 and type(choices[0]) is dict else None; message = choice.get("message") if type(choice) is dict else None
        if type(envelope) is not dict or type(choice) is not dict or type(message) is not dict or choice.get("finish_reason", "stop") != "stop" or any(row.get("status", "completed") not in {"completed", "success"} for row in (envelope, choice, message)) or any(key in row for row in (envelope, choice, message) for key in ("refusal", "tool_calls", "function_call", "tool_call_id", "error")): return None
        payload = _loads(message.get("content")) if type(message) is dict and type(message.get("content")) is str else None
        return _valid_payload(payload)
    except Exception:
        return None
class FlowSv9ShadowJsonProvider:
    """No-log, no-cache and no-retry component transport boundary."""
    __slots__ = ()

    def evaluate_component_json(self, request: Mapping[str, Any], *, environ: Mapping[str, object], model: object) -> ShadowProviderOutcome:
        try:
            if request_binding(request) is None or (selected := _model(model)) is None: return ShadowProviderOutcome.failure("request_invalid")
        except Exception:
            return ShadowProviderOutcome.failure("request_invalid")
        try:
            resolved = resolve_shadow_provider_config(shadow_provider_environment_snapshot(environ)); config = resolved.config
            if config is None: return ShadowProviderOutcome.failure(resolved.reason_code or "configuration_invalid_endpoint")
        except Exception:
            return ShadowProviderOutcome.failure("configuration_invalid_endpoint")
        try:
            outbound = Request(config.endpoint, data=_body(request, selected), headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}, method="POST")
        except Exception:
            return ShadowProviderOutcome.failure("request_invalid")
        try:
            with _open_once(outbound, config.timeout) as response:
                status = response.getcode()
                if type(status) is not int or not 200 <= status < 300: return ShadowProviderOutcome.failure("http_failure")
                data = response.read(_MAX_RESPONSE + 1)
        except HTTPError:
            return ShadowProviderOutcome.failure("http_failure")
        except (TimeoutError, socket.timeout):
            return ShadowProviderOutcome.failure("timeout")
        except URLError as exc:
            return ShadowProviderOutcome.failure("timeout" if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)) else "transport_failure")
        except Exception:
            return ShadowProviderOutcome.failure("transport_failure")
        if type(data) is not bytes or not data or len(data) > _MAX_RESPONSE: return ShadowProviderOutcome.failure("response_invalid")
        payload = _content(data)
        return ShadowProviderOutcome.success(payload) if payload is not None else ShadowProviderOutcome.failure("response_invalid")
# fmt: on
