"""Firecrawl acquisition adapter for Visual Signature.

This layer only acquires observable web data. Brand3-owned normalization,
taxonomy, and confidence logic live outside this adapter.
"""

from __future__ import annotations

from dataclasses import is_dataclass
from datetime import datetime
from typing import Any

from src.api_key_pool import ApiKeySource, normalize_api_keys
from src.collectors.web_collector import WebCollector, WebData
from src.config import FIRECRAWL_API_KEYS
from src.visual_signature._internal.utils import int_or_none as _int_or_none
from src.visual_signature.types import (
    ScreenshotSignal,
    VisualAcquisitionResult,
    VisualAssetCandidate,
    VisualSignatureInput,
)


class FirecrawlVisualSignatureAdapter:
    name = "firecrawl"

    def __init__(self, api_key: ApiKeySource = None, web_collector: WebCollector | None = None):
        configured_keys = api_key if api_key is not None else FIRECRAWL_API_KEYS
        self._has_api_keys = bool(normalize_api_keys(configured_keys))
        self.api_key = api_key
        self.web_collector = web_collector or WebCollector(api_key=configured_keys)

    def acquire(self, input_data: VisualSignatureInput) -> VisualAcquisitionResult:
        acquired_at = datetime.now().isoformat()
        if not self._has_api_keys:
            return VisualAcquisitionResult(
                adapter=self.name,
                requested_url=input_data.website_url,
                final_url=input_data.website_url,
                errors=["FIRECRAWL_API_KEY not set"],
                acquired_at=acquired_at,
            )
        try:
            web_data = self.web_collector.scrape(input_data.website_url)
        except Exception as exc:
            return VisualAcquisitionResult(
                adapter=self.name,
                requested_url=input_data.website_url,
                final_url=input_data.website_url,
                errors=[str(exc)],
                acquired_at=acquired_at,
            )
        return acquisition_from_web_data(web_data, adapter=self.name, acquired_at=acquired_at)


def acquisition_from_web_data(
    web_data: WebData | Any,
    *,
    adapter: str = "existing_web_data",
    acquired_at: str | None = None,
    screenshot_payload: dict[str, Any] | None = None,
) -> VisualAcquisitionResult:
    """Build acquisition data from Brand3's already-collected WebData shape."""
    acquired_at = acquired_at or datetime.now().isoformat()
    screenshot_url = (
        _get(web_data, "screenshot_path")
        or (screenshot_payload or {}).get("screenshot_url")
        or (screenshot_payload or {}).get("url")
    )
    screenshot = (
        ScreenshotSignal(url=str(screenshot_url), source="screenshot")
        if screenshot_url
        else None
    )
    metadata = {
        "title": _get(web_data, "title") or "",
        "description": _get(web_data, "meta_description") or "",
        "canonical_url": _get(web_data, "canonical_url") or "",
        "tech_stack": list(_get(web_data, "tech_stack") or []),
        "browser_status": _get(web_data, "browser_status"),
        "load_time_ms": _get(web_data, "load_time_ms"),
    }
    error = _get(web_data, "error") or ""
    return VisualAcquisitionResult(
        adapter=adapter,
        requested_url=_get(web_data, "url") or "",
        final_url=_get(web_data, "canonical_url") or _get(web_data, "url") or "",
        status_code=_get(web_data, "browser_status"),
        rendered_html=_get(web_data, "html") or "",
        raw_html=_get(web_data, "html") or "",
        markdown=_get(web_data, "markdown_content") or "",
        links=list(_get(web_data, "links") or []),
        images=_normalize_images(_get(web_data, "images") or []),
        screenshot=screenshot,
        metadata=metadata,
        warnings=[],
        errors=[str(error)] if error else [],
        acquired_at=acquired_at,
    )


def _normalize_images(items: list[Any]) -> list[VisualAssetCandidate]:
    candidates: list[VisualAssetCandidate] = []
    for item in items:
        if isinstance(item, str):
            candidates.append(
                VisualAssetCandidate(
                    url=item,
                    source="images",
                    role_hint=_role_hint_from_text(item),
                )
            )
            continue
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("src")
        if not url:
            continue
        candidates.append(
            VisualAssetCandidate(
                url=str(url),
                alt=str(item.get("alt") or "") or None,
                width=_int_or_none(item.get("width")),
                height=_int_or_none(item.get("height")),
                source="images",
                role_hint=_role_hint_from_text(f"{url} {item.get('alt') or ''}"),
            )
        )
    return candidates


def _role_hint_from_text(value: str) -> str:
    lowered = value.lower()
    if any(token in lowered for token in ("logo", "brandmark", "wordmark")):
        return "logo"
    if any(token in lowered for token in ("icon", "favicon")):
        return "icon"
    if any(token in lowered for token in ("illustration", "graphic")):
        return "illustration"
    if any(token in lowered for token in ("background", "hero")):
        return "background"
    return "unknown"


def _get(value: Any, field_name: str) -> Any:
    if isinstance(value, dict):
        return value.get(field_name)
    if is_dataclass(value) or hasattr(value, field_name):
        return getattr(value, field_name, None)
    return None
