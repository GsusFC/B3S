"""Runtime primitives for LLM analyzer orchestration internals."""

from __future__ import annotations

import os
from typing import Any

from src.config import LLM_BASE_URL, LLM_MODEL
from src.features import llm_analyzer_support as _llm_support

PROMPT_VERSION = _llm_support.PROMPT_VERSION
LLM_CALL_TIMEOUT_SECONDS = _llm_support.LLM_CALL_TIMEOUT_SECONDS


def _safe_excerpt(value: str | None, max_chars: int = 500) -> str:
    text = "" if value is None else str(value)
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1]}…"


def _current_impl_module():
    from src.features import llm_analyzer as llm_analyzer

    return llm_analyzer


def llm_failure_reason(llm, default: str) -> str:
    reason = getattr(llm, "last_failure_reason", None)
    if reason in {
        "llm_timeout",
        "llm_error",
        "transport_error",
        "schema_validation_error",
        "provider_http_error",
    }:
        return reason
    return default


class _LLMAnalyzerRuntime:
    """Low-level LLM analyzer methods split out from the monolithic implementation."""

    @staticmethod
    def _resolve_api_key(api_key: str | None) -> str:
        if api_key:
            return api_key
        return (
            os.environ.get("BRAND3_LLM_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("OPENROUTER_API_KEY", "")
        )

    @staticmethod
    def _resolve_base_url(base_url: str | None) -> str:
        if base_url:
            return base_url
        return os.environ.get("BRAND3_LLM_BASE_URL", LLM_BASE_URL)

    @staticmethod
    def _resolve_model(model: str | None) -> str:
        if model:
            return model
        return os.environ.get("BRAND3_LLM_MODEL", LLM_MODEL)

    def _record_failure(
        self,
        reason: str,
        error: str,
        *,
        error_type: str | None = None,
        response_empty: bool = False,
        json_parse_error: bool = False,
    ) -> None:
        self.last_failure_reason = reason
        provider_payload = _llm_support._provider_error_payload(error)
        self.call_failures.append(
            {
                "reason": reason,
                "error": error[:200],
                "error_type": error_type or _llm_support._llm_error_type(reason, error),
                "http_status": provider_payload["http_status"],
                "provider_error_code": provider_payload["provider_error_code"],
                "provider_error_message": provider_payload["provider_error_message"],
                "response_empty": bool(response_empty),
                "json_parse_error": bool(json_parse_error),
                "model": self.model,
                "base_url": self.base_url,
            }
        )

    def _clear_failure(self) -> None:
        self.last_failure_reason = None

    def _record_usage_observation(
        self,
        *,
        event: str,
        response_type: str,
        cache_key: str,
        status: str | None = None,
        max_tokens: int | None = None,
        schema_name: str | None = None,
        request_variant: str | None = None,
        usage_metadata: dict[str, Any] | None = None,
    ) -> None:
        observations = getattr(self, "usage_observations", None)
        if not isinstance(observations, list):
            return
        record = {
            "event": event,
            "model": self.model,
            "base_url": self.base_url,
            "response_type": response_type,
            "cache_key": cache_key,
            "cache_key_prefix": cache_key[:12],
            "status": status,
            "max_tokens": max_tokens,
            "schema_name": schema_name or "",
            "request_variant": request_variant or "",
            "usage_metadata_available": bool(usage_metadata),
            "usage_metadata": usage_metadata or {},
        }
        observations.append(record)

    def usage_observation_summary(self) -> dict[str, Any]:
        observations = getattr(self, "usage_observations", [])
        if not isinstance(observations, list):
            observations = []
        return {
            "model": self.model,
            "base_url": self.base_url,
            "cache_hits": int(getattr(self, "cache_hits", 0) or 0),
            "cache_misses": int(getattr(self, "cache_misses", 0) or 0),
            "cache_writes": int(getattr(self, "cache_writes", 0) or 0),
            "provider_calls": sum(1 for item in observations if item.get("event") == "provider_call"),
            "usage_metadata_available": any(
                bool(item.get("usage_metadata_available")) for item in observations
            ),
            "observations": observations,
        }

    def _cache_key(
        self,
        response_type: str,
        system: str,
        user: str,
        max_tokens: int,
        *,
        schema_name: str | None = None,
        temperature: float = 0.1,
        json_schema: dict[str, Any] | None = None,
        strict_schema: bool = True,
    ) -> str:
        payload = {
            "prompt_version": PROMPT_VERSION,
            "model": self.model,
            "response_type": response_type,
            "schema_name": schema_name or "",
            "system": system,
            "user": user,
            "max_tokens": max_tokens,
            "temperature": float(temperature),
            "json_schema": json_schema or {},
            "strict_schema": bool(strict_schema),
        }
        return _llm_support._llm_cache_digest(payload)

    def _cache_get(self, cache_key: str, response_type: str):
        if not self.use_cache:
            return None
        try:
            from src.storage.sqlite_store import SQLiteStore

            store = SQLiteStore(_current_impl_module().BRAND3_DB_PATH)
            try:
                cached = store.get_llm_cache(cache_key)
            finally:
                store.close()
        except Exception:
            return None
        if not cached or cached.get("response_type") != response_type:
            return None
        self.cache_hits += 1
        self._record_usage_observation(
            event="cache_hit",
            response_type=response_type,
            cache_key=cache_key,
        )
        if response_type == "json":
            return cached.get("response_json") or {}
        return cached.get("response_text") or ""

    def _cache_save(self, cache_key: str, response_type: str, value) -> None:
        if not self.use_cache:
            return
        if value in ("", None, {}):
            return
        try:
            from src.storage.sqlite_store import SQLiteStore

            store = SQLiteStore(_current_impl_module().BRAND3_DB_PATH)
            try:
                store.save_llm_cache(
                    cache_key=cache_key,
                    prompt_version=PROMPT_VERSION,
                    model=self.model,
                    response_type=response_type,
                    response_json=value if response_type == "json" else None,
                    response_text=value if response_type == "text" else None,
                )
            finally:
                store.close()
            self.cache_writes += 1
            self._record_usage_observation(
                event="cache_write",
                response_type=response_type,
                cache_key=cache_key,
            )
        except Exception:
            return
