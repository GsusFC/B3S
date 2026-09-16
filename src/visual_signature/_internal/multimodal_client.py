"""Gemini Vision request helpers for Visual Signature."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from pathlib import Path
from typing import Any, Protocol, Tuple

from src.config import (
    BRAND3_LLM_API_KEY,
    BRAND3_VISUAL_SIGNATURE_MODEL,
    BRAND3_VISUAL_SIGNATURE_TIMEOUT_SECONDS,
    LLM_BASE_URL,
)
from src.features.llm_analyzer import LLM_CALL_TIMEOUT_SECONDS, _run_llm_http_call


class MultimodalRequestExecutor(Protocol):
    def __call__(self, *, url: str, payload: dict[str, Any], headers: dict[str, str], timeout_seconds: int) -> tuple[str, str]:
        ...


def effective_timeout() -> int:
    if BRAND3_VISUAL_SIGNATURE_TIMEOUT_SECONDS > 0:
        return BRAND3_VISUAL_SIGNATURE_TIMEOUT_SECONDS
    return LLM_CALL_TIMEOUT_SECONDS


# A pretty-printed 16-field semantics object with sentence lists exceeds 1200
# output tokens; truncated JSON fails to parse and the whole audit is lost.
MULTIMODAL_MAX_OUTPUT_TOKENS = 4096


def build_multimodal_payload(*, prompt_template: str, system_preamble: str, encoded_image: str, mime_type: str, brand_name: str) -> dict[str, Any]:
    prompt = prompt_template.format(brand_name=brand_name)

    return {
        "model": BRAND3_VISUAL_SIGNATURE_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": system_preamble + prompt,
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{encoded_image}",
                        },
                    },
                ],
            }
        ],
        "max_tokens": MULTIMODAL_MAX_OUTPUT_TOKENS,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }


def build_cache_key(*, model: str, prompt_version: str, brand_name: str, screenshot_bytes: bytes) -> str:
    payload = {
        "prompt_version": prompt_version,
        "model": model,
        "brand_name": brand_name,
        "screenshot_sha256": hashlib.sha256(screenshot_bytes).hexdigest(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def mime_type_for_path(path: Path) -> str:
    mime_type, _encoding = mimetypes.guess_type(os.fspath(path))
    if mime_type in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        return mime_type
    return "image/png"


def run_multimodal_request(
    *,
    payload: dict[str, Any],
    url: str | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: int | None = None,
) -> Tuple[str, str]:
    effective_headers = headers
    if effective_headers is None:
        effective_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {BRAND3_LLM_API_KEY}",
        }

    effective_url = url or f"{LLM_BASE_URL}/chat/completions"
    effective_timeout_seconds = timeout_seconds if timeout_seconds is not None else effective_timeout()

    result = _run_llm_http_call(
        url=effective_url,
        payload=json.dumps(payload).encode("utf-8"),
        headers=effective_headers,
        timeout_seconds=effective_timeout_seconds,
    )
    # The shared transport returns (status, content, usage); this contract
    # exposes only status and content, so callers can unpack two values.
    return result[0], result[1]


def run_multimodal_http_call(
    *,
    url: str,
    payload: bytes,
    headers: dict[str, str],
    timeout_seconds: int,
    request_executor: MultimodalRequestExecutor | None = None,
) -> tuple[str, str]:
    executor = request_executor or run_multimodal_request
    payload_obj = json.loads(payload.decode("utf-8"))
    return executor(url=url, payload=payload_obj, headers=headers, timeout_seconds=timeout_seconds)
