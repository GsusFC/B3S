"""
LLM-based brand analysis.

Uses LLM to make subjective judgments that keyword matching can't:
- Is the brand's language unique or generic?
- What category/positioning does the brand claim?
- How does third-party perception compare to self-description?
- What are the brand's distinctive concepts?

Provider is configured via src.config (OpenAI-compatible API).
"""

import json
import logging
import os
import urllib.parse
from typing import Any

from src.config import BRAND3_DB_PATH
from src.features import llm_analyzer_support as _llm_support
from src.features import llm_analyzer_runtime as _llm_runtime

PROMPT_VERSION = _llm_support.PROMPT_VERSION
LLM_CALL_TIMEOUT_SECONDS = _llm_support.LLM_CALL_TIMEOUT_SECONDS
STRUCTURED_RESEARCH_PACK_LIMIT = _llm_support.STRUCTURED_RESEARCH_PACK_LIMIT

_llm_prompt_input = _llm_support._llm_prompt_input
_run_llm_http_call = _llm_support._run_llm_http_call
_run_gemini_http_call = _llm_support._run_gemini_http_call
_transport_debug_enabled = _llm_support._transport_debug_enabled
_chat_completions_url = _llm_support._chat_completions_url
_gemini_generate_content_url = _llm_support._gemini_generate_content_url
_redacted_headers = _llm_support._redacted_headers
_body_top_level_keys = _llm_support._body_top_level_keys
_looks_like_transport_error = _llm_support._looks_like_transport_error
_validate_json_schema = _llm_support._validate_json_schema
_json_response_format = _llm_support._json_response_format
_parse_json_content = _llm_support._parse_json_content
_safe_excerpt = _llm_runtime._safe_excerpt
llm_failure_reason = _llm_runtime.llm_failure_reason


_LOG = logging.getLogger(__name__)


def _normalize_usage_metadata(usage: Any) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None
    out: dict[str, Any] = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "promptTokenCount",
        "candidatesTokenCount",
        "totalTokenCount",
        "cachedContentTokenCount",
        "thoughtsTokenCount",
    ):
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            out[key] = value
    return out or None


def _llm_call_result(value: Any) -> tuple[str, str, dict[str, Any] | None]:
    """Accept legacy (status, content) pairs from test doubles."""

    if isinstance(value, tuple) and len(value) == 2:
        return value[0], value[1], None
    return value


class LLMAnalyzer(_llm_runtime._LLMAnalyzerRuntime):
    """LLM-powered brand content analyzer."""

    def __init__(self, api_key: str = None, base_url: str = None, model: str = None):
        self.api_key = self._resolve_api_key(api_key)
        self.base_url = self._resolve_base_url(base_url)
        self.model = self._resolve_model(model)
        self.cache_hits = 0
        self.cache_misses = 0
        self.cache_writes = 0
        self.use_cache = os.environ.get("BRAND3_LLM_CACHE_ENABLED", "true").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self.timeout_seconds = LLM_CALL_TIMEOUT_SECONDS
        self.last_failure_reason: str | None = None
        self.last_raw_response: str | None = None
        self.call_failures: list[dict[str, Any]] = []
        self.last_request_debug: dict[str, Any] | None = None
        self.usage_observations: list[dict[str, Any]] = []

    def _call(self, system: str, user: str, max_tokens: int = 8000) -> str:
        """Make an LLM call via the OpenAI-compatible endpoint.

        Default `max_tokens` is wide enough to accommodate thinking models
        (Gemini 3.x) that consume part of the budget on internal reasoning
        before emitting content.
        """
        if not self.api_key:
            return ""

        cache_key = self._cache_key("text", system, user, max_tokens, temperature=0.1)
        cached = self._cache_get(cache_key, "text")
        if cached is not None:
            self._clear_failure()
            return cached
        self.cache_misses += 1
        self._record_usage_observation(
            event="cache_miss",
            response_type="text",
            cache_key=cache_key,
            max_tokens=max_tokens,
        )

        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }
        payload = json.dumps(body).encode()

        status, content, usage_payload = _llm_call_result(
            _run_llm_http_call(
                url=_chat_completions_url(self.base_url),
                payload=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                timeout_seconds=self.timeout_seconds,
            )
        )
        self._record_usage_observation(
            event="provider_call",
            response_type="text",
            cache_key=cache_key,
            status=status,
            max_tokens=max_tokens,
            usage_metadata=_normalize_usage_metadata(usage_payload),
        )
        if status == "ok":
            if not content:
                self._record_failure(
                    "llm_error",
                    "empty_provider_response",
                    error_type="empty_response",
                    response_empty=True,
                )
                return ""
            self._clear_failure()
            self._cache_save(cache_key, "text", content)
            return content

        if status == "timeout":
            reason = "llm_timeout"
        elif _looks_like_transport_error(content):
            reason = "transport_error"
        else:
            reason = "llm_error"
        self._record_failure(reason, content)
        _LOG.warning("llm call failed", extra={"reason": reason, "error": _safe_excerpt(content)})
        return ""

    def _call_json(
        self,
        system: str,
        user: str,
        max_tokens: int = 8000,
        *,
        json_schema: dict[str, Any] | None = None,
        schema_name: str | None = None,
        strict_schema: bool = True,
        timeout_seconds: int | None = None,
        temperature: float | None = None,
        sanitized_diagnostics: bool = False,
    ) -> dict:
        """Make an LLM call expecting strict JSON response.

        Uses JSON mode by default. When `json_schema` is provided, uses the
        OpenAI-compatible `json_schema` response_format and falls back to plain
        `json_object` if the provider rejects schema mode.
        """
        if not self.api_key:
            return {}
        if sanitized_diagnostics: self.last_raw_response = ""

        normalized_schema_name = schema_name if json_schema else None
        effective_temperature = 0.1 if temperature is None else float(temperature)
        cache_key = self._cache_key(
            "json",
            system,
            user,
            max_tokens,
            schema_name=normalized_schema_name,
            temperature=effective_temperature,
            json_schema=json_schema,
            strict_schema=strict_schema,
        )
        cached = self._cache_get(cache_key, "json")
        if cached is not None:
            self._clear_failure()
            return cached
        self.cache_misses += 1
        self._record_usage_observation(
            event="cache_miss",
            response_type="json",
            cache_key=cache_key,
            max_tokens=max_tokens,
            schema_name=normalized_schema_name,
        )

        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": effective_temperature,
            "response_format": _json_response_format(
                json_schema=json_schema,
                schema_name=schema_name,
                strict_schema=strict_schema,
            ),
        }

        effective_timeout = self.timeout_seconds if timeout_seconds is None else int(timeout_seconds)
        payload = json.dumps(body).encode()
        final_url = _chat_completions_url(self.base_url)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if _transport_debug_enabled():
            self.last_request_debug = {
                "base_url_repr": repr(self.base_url),
                "path_repr": repr("/chat/completions"),
                "final_url_repr": repr(final_url),
                "parsed_url": {
                    "scheme": urllib.parse.urlparse(final_url).scheme,
                    "netloc": urllib.parse.urlparse(final_url).netloc,
                    "path": urllib.parse.urlparse(final_url).path,
                    "params": urllib.parse.urlparse(final_url).params,
                    "query": urllib.parse.urlparse(final_url).query,
                    "fragment": urllib.parse.urlparse(final_url).fragment,
                },
                "method": "POST",
                "headers": _redacted_headers(headers),
                "body_top_level_keys": _body_top_level_keys(payload),
                "timeout_seconds": effective_timeout,
            }
            _LOG.debug("llm transport debug", extra={"request_debug": self.last_request_debug})
        status, content, usage_payload = _llm_call_result(
            _run_llm_http_call(
                url=final_url,
                payload=payload,
                headers=headers,
                timeout_seconds=effective_timeout,
            )
        )
        self._record_usage_observation(
            event="provider_call",
            response_type="json",
            cache_key=cache_key,
            status=status,
            max_tokens=max_tokens,
            schema_name=normalized_schema_name,
            usage_metadata=_normalize_usage_metadata(usage_payload),
        )
        if status != "ok" and json_schema is not None:
            # Provider compatibility varies; keep production safe by falling back
            # to JSON mode while preserving schema-specific cache separation.
            if status != "transport_error":
                fallback_body = dict(body)
                fallback_body["response_format"] = {"type": "json_object"}
                status, content, usage_payload = _llm_call_result(
                    _run_llm_http_call(
                        url=final_url,
                        payload=json.dumps(fallback_body).encode(),
                        headers=headers,
                        timeout_seconds=effective_timeout,
                    )
                )
                self._record_usage_observation(
                    event="provider_call",
                    response_type="json",
                    cache_key=cache_key,
                    status=status,
                    max_tokens=max_tokens,
                    schema_name=normalized_schema_name,
                    request_variant="json_object_fallback",
                    usage_metadata=_normalize_usage_metadata(usage_payload),
                )
        if status != "ok":
            if status == "timeout":
                reason = "llm_timeout"
            elif status == "transport_error" or _looks_like_transport_error(content):
                reason = "transport_error"
            elif status == "http_error" or (content or "").startswith("HTTP "):
                reason = "provider_http_error"
            else:
                reason = "llm_error"
            if sanitized_diagnostics: content = ""
            self._record_failure(reason, content)
            self.last_raw_response = content
            _LOG.warning("llm json call failed", extra={"reason": reason, "error": _safe_excerpt(content)})
            return {}

        if not content:
            self._record_failure(
                "llm_error",
                "empty_provider_response",
                error_type="empty_response",
                response_empty=True,
            )
            self.last_raw_response = ""
            return {}

        # Belt-and-suspenders: strip markdown fencing if the model still added it.
        content = content.strip()
        self.last_raw_response = "" if sanitized_diagnostics else content
        if content.startswith("```"):
            content = content.split("\n", 1)[1]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()

        try:
            parsed = _parse_json_content(content)
            if json_schema is not None and strict_schema:
                schema_error = _validate_json_schema(parsed, json_schema)
                if schema_error:
                    if sanitized_diagnostics: schema_error = "schema_validation_error"
                    self._record_failure(
                        "schema_validation_error",
                        schema_error,
                        error_type="schema_validation_error",
                    )
                    return {}
            self._cache_save(cache_key, "json", parsed)
            self._clear_failure()
            return parsed
        except json.JSONDecodeError as e:
            _LOG.warning(
                "llm json parse failed",
                extra={"error": "" if sanitized_diagnostics else str(e), "snippet": "" if sanitized_diagnostics else _safe_excerpt(content, max_chars=220)},
            )
            self._record_failure(
                "llm_error",
                "json_parse_error" if sanitized_diagnostics else str(e),
                error_type="json_parse_error",
                json_parse_error=True,
            )
            return {}

    def _call_json_gemini_native(
        self,
        system: str,
        user: str,
        max_tokens: int = 8000,
        *,
        json_schema: dict[str, Any],
        schema_name: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict:
        """Call Gemini's native structured output API for JSON Schema responses."""
        if not self.api_key:
            return {}

        cache_key = self._cache_key(
            "json_gemini_native",
            system,
            user,
            max_tokens,
            schema_name=schema_name or "brand3_json_response",
            temperature=0.0,
            json_schema=json_schema,
            strict_schema=True,
        )
        cached = self._cache_get(cache_key, "json")
        if cached is not None:
            self._clear_failure()
            return cached
        self.cache_misses += 1
        self._record_usage_observation(
            event="cache_miss",
            response_type="json_gemini_native",
            cache_key=cache_key,
            max_tokens=max_tokens,
            schema_name=schema_name or "brand3_json_response",
        )

        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0.0,
                "maxOutputTokens": max_tokens,
                "responseFormat": {
                    "text": {
                        "mimeType": "APPLICATION_JSON",
                        "schema": json_schema,
                    }
                },
            },
        }
        effective_timeout = self.timeout_seconds if timeout_seconds is None else int(timeout_seconds)
        final_url = _gemini_generate_content_url(self.base_url, self.model)
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }
        payload = json.dumps(body).encode("utf-8")
        status, content, usage_payload = _llm_call_result(
            _run_gemini_http_call(
                url=final_url,
                payload=payload,
                headers=headers,
                timeout_seconds=effective_timeout,
            )
        )
        self._record_usage_observation(
            event="provider_call",
            response_type="json_gemini_native",
            cache_key=cache_key,
            status=status,
            max_tokens=max_tokens,
            schema_name=schema_name or "brand3_json_response",
            usage_metadata=_normalize_usage_metadata(usage_payload),
        )
        if status != "ok":
            if status == "timeout":
                reason = "llm_timeout"
            elif status == "transport_error" or _looks_like_transport_error(content):
                reason = "transport_error"
            elif status == "http_error" or (content or "").startswith("HTTP "):
                reason = "provider_http_error"
            else:
                reason = "llm_error"
            self._record_failure(reason, content)
            self.last_raw_response = content
            _LOG.warning(
                "gemini native json call failed",
                extra={"reason": reason, "error": _safe_excerpt(content)},
            )
            return {}

        if not content:
            self._record_failure(
                "llm_error",
                "empty_provider_response",
                error_type="empty_response",
                response_empty=True,
            )
            self.last_raw_response = ""
            return {}

        content = content.strip()
        self.last_raw_response = content
        try:
            parsed = _parse_json_content(content)
            schema_error = _validate_json_schema(parsed, json_schema)
            if schema_error:
                self._record_failure(
                    "schema_validation_error",
                    schema_error,
                    error_type="schema_validation_error",
                )
                return {}
            self._cache_save(cache_key, "json", parsed)
            self._clear_failure()
            return parsed
        except json.JSONDecodeError as e:
            _LOG.warning(
                "gemini native json parse failed",
                extra={"error": str(e), "snippet": _safe_excerpt(content, max_chars=220)},
            )
            self._record_failure(
                "llm_error",
                str(e),
                error_type="json_parse_error",
                json_parse_error=True,
            )
            return {}

    def analyze_positioning_clarity(
        self, web_content: str, brand_name: str, competitor_snippets: list[str] | None = None
    ) -> dict:
        """LLM judgment for positioning clarity with literal evidence."""
        content = _llm_prompt_input(web_content, default_limit=3000)
        competitor_block = ""
        if competitor_snippets:
            competitor_block = "\n\nCompetitor context:\n---\n" + "\n---\n".join(
                snippet[:500] for snippet in competitor_snippets[:3]
            )

        return self._call_json(
            system=(
                "You are a brand positioning analyst. Return ONLY valid JSON. "
                "You must use literal evidence and respect source boundaries."
            ),
            user=f"""Analyze the positioning clarity of this brand.

Brand: {brand_name}
Evidence input:
---
{content}
---{competitor_block}

Instructions:
- The input may be a Structured Brand Research Pack rather than raw website copy.
- Treat owned/core evidence as the brand's self-description.
- Treat proof points, press, founder context, and third-party mentions as context, not as self-positioning.
- Never use rejected noise, page chrome, navigation, cookie text, or CTAs as positioning evidence.
- Evidence gaps and confidence notes are negative evidence. Use them to lower confidence/verdict when relevant.
- Distinguish:
  - clear: the position is articulated concretely and sustained in the content
  - diffuse: it gestures at a position but loses focus
  - generic: template SaaS language with little real positioning
  - unclear: too little usable owned/self evidence, even if press/proof context exists
- Evidence quotes must be literal snippets from the evidence input, not paraphrases.

Return JSON with this exact structure:
{{
  "clarity_score": 0,
  "verdict": "clear" | "diffuse" | "generic" | "unclear",
  "stated_position": "one sentence",
  "target_audience": "one phrase",
  "differentiator_claimed": "one phrase",
  "evidence": [
    {{"quote": "literal quote", "signal": "clear" | "generic" | "aspirational"}}
  ],
  "reasoning": "1-2 sentences"
}}"""
        )

    def analyze_uniqueness(
        self, web_content: str, brand_name: str, competitor_snippets: list[str] | None = None
    ) -> dict:
        """LLM judgment for brand uniqueness vs generic language."""
        content = _llm_prompt_input(web_content, default_limit=3000)
        competitor_block = ""
        if competitor_snippets:
            competitor_block = "\n\nCompetitor context:\n---\n" + "\n---\n".join(
                snippet[:500] for snippet in competitor_snippets[:3]
            )

        return self._call_json(
            system=(
                "You are a brand differentiation analyst. Return ONLY valid JSON. "
                "Distinguish generic SaaS template language from ownable vocabulary."
            ),
            user=f"""Analyze how unique this brand's language is.

Brand: {brand_name}
Evidence input:
---
{content}
---{competitor_block}

Instructions:
- The input may be a Structured Brand Research Pack rather than raw website copy.
- Judge uniqueness of the brand's own language, not uniqueness of the market category alone.
- Distinguish generic SaaS language ("cutting edge", "seamless", "revolutionary").
- Distinguish empty aspirational language ("we empower", "unlock potential").
- Highlight authentic brand vocabulary and repeated ownable terms.
- Do not mark a brand unique because press/proof points describe traction or funding.
- Use competitor overlap to penalize vocabulary that mirrors category peers.
- Never use rejected noise, page chrome, navigation, cookie text, or CTAs as uniqueness evidence.

Return JSON with this exact structure:
{{
  "uniqueness_score": 0,
  "verdict": "highly_unique" | "moderately_unique" | "derivative" | "generic" | "unclear",
  "unique_phrases": ["phrase"],
  "generic_phrases": ["phrase"],
  "brand_vocabulary": ["term"],
  "competitor_overlap_signals": ["signal"],
  "reasoning": "1-2 sentences"
}}"""
        )

    def analyze_messaging_consistency(
        self,
        web_content: str,
        third_party_mentions: list[dict],
        brand_name: str,
    ) -> dict:
        """Compare self-description (web) with third-party descriptions (mentions).

        Returns verdict with literal quotes in `gaps` as evidence.
        """
        # REVIEW: método nuevo para messaging_consistency de coherencia.
        if not web_content or not isinstance(third_party_mentions, list):
            return {}

        content = _llm_prompt_input(web_content, default_limit=3000)
        lines = []
        for i, m in enumerate(third_party_mentions[:8], start=1):
            text = (m.get("text") or "")[:400].replace("\n", " ").strip()
            url = m.get("url") or ""
            title = (m.get("title") or "").replace("\n", " ").strip()
            source_class = (m.get("source_class") or "unknown").replace("\n", " ").strip()
            relation = (m.get("relation") or "unknown").replace("\n", " ").strip()
            if not text and not title:
                continue
            lines.append(f"[{i}] {url}\nsource_class={source_class}; relation={relation}\n{title}\n{text}")
        mentions_block = "\n---\n".join(lines) if lines else "(no mentions available)"

        return self._call_json(
            system=(
                "You are a brand coherence analyst. Compare how the brand describes "
                "itself against how third parties describe it. You quote sources "
                "literally. Return ONLY valid JSON."
            ),
            user=f"""Analyse whether "{brand_name}" describes itself consistently with how others describe it.

Brand's own website copy:
---
{content}
---

Third-party mentions:
---
{mentions_block}
---

Rules:
- The brand evidence may be a Structured Brand Research Pack. Use it as the self-description side.
- Use only the Third-party mentions section as external perception.
- Do not treat owned claims, proof points, founder context, or rejected noise inside the Research Pack as third-party validation.
- Return literal quotes in `gaps`, not paraphrase.
- If fewer than 2 third-party mentions are useful, return `verdict: "unclear"` and empty `gaps`.
- Mentions with source_class=owned or relation=same_root_surface/audited_surface are not independent; do not count them as useful third-party mentions.
- Ignore mentions that are clearly NOT about "{brand_name}" (scraping false positives).

Return JSON with this exact structure:
{{
    "consistency_score": 0-100,
    "verdict": "aligned" | "partial_gap" | "divergent" | "unclear",
    "self_category": "how the brand describes itself in one phrase",
    "third_party_category": "how others describe it in one phrase",
    "aligned_themes": ["themes both agree on"],
    "gaps": [
        {{"self_says": "literal quote from website", "third_party_says": "literal quote from a mention", "source_url": "the mention url"}}
    ],
    "reasoning": "1-2 sentences explaining the verdict"
}}"""
        )

    def analyze_tone_consistency(
        self,
        web_content: str,
        third_party_snippets: list[dict],
        brand_name: str,
    ) -> dict:
        """Assess whether tone on the brand's surface matches third-party tone."""
        # REVIEW: método nuevo para tone_consistency de coherencia.
        if not web_content:
            return {}

        content = _llm_prompt_input(web_content, default_limit=2500)
        lines = []
        for i, m in enumerate((third_party_snippets or [])[:5], start=1):
            text = (m.get("text") or "")[:300].replace("\n", " ").strip()
            url = m.get("url") or ""
            source_class = (m.get("source_class") or "unknown").replace("\n", " ").strip()
            relation = (m.get("relation") or "unknown").replace("\n", " ").strip()
            if not text:
                continue
            lines.append(f"[{i}] {url}\nsource_class={source_class}; relation={relation}\n{text}")
        mentions_block = "\n---\n".join(lines) if lines else "(no third-party snippets)"

        return self._call_json(
            system=(
                "You are a brand tone analyst. Describe the tone of the brand's own "
                "copy and the tone of third-party mentions, and judge whether they "
                "match. Quote sources literally. Return ONLY valid JSON."
            ),
            user=f"""Assess tone consistency for "{brand_name}".

Brand's own website copy:
---
{content}
---

Third-party mentions:
---
{mentions_block}
---

Rules:
- The brand evidence may be a Structured Brand Research Pack. Use it as the brand's own tone side.
- Use only the Third-party mentions section for external tone.
- Tone examples MUST be literal quotes.
- Return `gap_signal: "none"` only when brand tone and at least one useful independent mention tone are visibly aligned.
- If no useful third-party material, return `gap_signal: "mild"` with conservative reasoning rather than pretending external tone is known.
- If contradictions exist, return `gap_signal: "strong"` and a lower score.
- Do not use rejected noise, page chrome, navigation, cookie text, or CTAs as tone evidence.

Return JSON with this exact structure:
{{
    "tone_consistency_score": 0-100,
    "self_tone": "description of the tone in the website",
    "third_party_tone": "how the mentions sound about the brand",
    "gap_signal": "none" | "mild" | "strong",
    "examples": [
        {{"source": "web" | "mention", "quote": "literal quote", "tone_marker": "what signals the tone"}}
    ],
    "reasoning": "1-2 sentences"
}}"""
        )

    def analyze_brand_sentiment(self, mentions: list[dict], brand_name: str) -> dict:
        """Unified sentiment + controversy analysis for Percepción.

        Reads up to 15 third-party mentions and returns a structured verdict
        with literal quotes as evidence. Flags controversy explicitly so the
        caller can cap the score without needing a separate rule.
        """
        # REVIEW: método nuevo para brand_sentiment de percepcion.
        if not mentions:
            return {}

        lines = []
        for i, m in enumerate(mentions[:15], start=1):
            text = (m.get("text") or "")[:400].replace("\n", " ").strip()
            url = m.get("url") or ""
            title = (m.get("title") or "").replace("\n", " ").strip()
            if not text and not title:
                continue
            lines.append(f"[{i}] {url}\n{title}\n{text}")
        mentions_block = "\n---\n".join(lines) if lines else "(no mentions available)"

        return self._call_json(
            system=(
                "You are a brand perception analyst. Read third-party mentions "
                "and decide whether public sentiment towards the brand is "
                "positive, mixed, negative, or unclear. Flag serious "
                "controversies separately from ordinary product criticism. "
                "Quote sources literally. Return ONLY valid JSON."
            ),
            user=f"""Analyse public sentiment about "{brand_name}" based on these mentions.

Mentions:
---
{mentions_block}
---

Rules:
- Evidence MUST be literal quotes from the mentions above, not paraphrase.
- Distinguish legitimate product criticism (expensive, confusing UX) from serious controversy (lawsuits, scandals, data breaches, regulatory action).
- Set `controversy_detected: true` only for serious issues, not ordinary complaints.
- If fewer than 3 useful mentions, return `verdict: "unclear"`.
- Ignore mentions that are clearly NOT about "{brand_name}" (scraping false positives).

Return JSON with this exact structure:
{{
    "sentiment_score": 0-100,
    "verdict": "positive" | "mixed" | "negative" | "unclear",
    "overall_tone": "one sentence describing how people talk about the brand",
    "positive_themes": ["recurring positive themes"],
    "negative_themes": ["recurring negative themes or criticisms"],
    "evidence": [
        {{"quote": "literal quote from a mention", "source_url": "the url", "signal": "positive" | "negative" | "neutral"}}
    ],
    "controversy_detected": true | false,
    "controversy_details": "concrete description if true, null if false",
    "reasoning": "1-2 sentences"
}}"""
        )

    def analyze_momentum(self, mentions: list[dict], brand_name: str) -> dict:
        """
        Is the brand actively building or drifting into maintenance?

        Reads third-party mentions (last ~6 months recommended) and returns a
        structured verdict with literal quotes as evidence.

        mentions: list of dicts with keys {text, url, published_date}.
        Returns JSON-shaped dict: {momentum_score, verdict, evidence[], reasoning}.
        """
        # REVIEW: método nuevo añadido al LLMAnalyzer para soportar la feature
        # `momentum` de vitalidad. Sigue el patrón de los otros `analyze_*`.
        if not mentions:
            return {}

        lines = []
        for i, m in enumerate(mentions[:15], start=1):
            text = (m.get("text") or "")[:400].replace("\n", " ").strip()
            url = m.get("url") or ""
            date = m.get("published_date") or "unknown"
            if not text:
                continue
            lines.append(f"[{i}] ({date}) {url}\n{text}")
        mentions_block = "\n---\n".join(lines)

        if not mentions_block:
            return {}

        return self._call_json(
            system=(
                "You are a brand momentum analyst. You read recent third-party "
                "mentions and decide whether a brand is actively building, merely "
                "maintaining, or declining. You quote sources literally. Return "
                "ONLY valid JSON."
            ),
            user=f"""Assess the momentum of the brand "{brand_name}" based on these recent mentions.

Mentions:
---
{mentions_block}
---

Rules:
- Look for signals of active construction (new launches, key hires, expansion,
  strategic partnerships, significant investment) vs signals of maintenance or
  decline (media silence, layoffs, customer loss, unanswered controversies).
- Evidence MUST be literal quotes pulled from the mentions above, not paraphrase.
- If evidence is ambiguous or insufficient, return verdict "unclear" with a low score.
- Ignore mentions that are clearly NOT about "{brand_name}" (scraping false positives).

Return JSON with this exact structure:
{{
    "momentum_score": 0-100,
    "verdict": "building" | "maintaining" | "declining" | "unclear",
    "evidence": [
        {{"quote": "literal quote from a mention", "source_url": "the url", "signal": "positive" | "negative" | "neutral"}}
    ],
    "reasoning": "1-2 sentences explaining the verdict"
}}"""
        )
