"""Social collection runtime helpers."""

from __future__ import annotations

import multiprocessing as mp
import queue

from src.api_key_pool import ApiKeySource
from src.collectors.social_collector import SocialCollector, SocialData


_SOCIAL_COLLECTION_TIMEOUT_SECONDS = 25


def _social_collect_worker(
    output_queue,
    api_key: ApiKeySource,
    brand_name: str,
    web_content: str,
) -> None:
    try:
        data = SocialCollector(api_key=api_key).collect(brand_name, web_content)
        output_queue.put(("ok", data))
    except Exception as exc:
        output_queue.put(("error", str(exc)))


def _collect_social_with_budget(
    brand_name: str,
    web_content: str,
    *,
    api_key: ApiKeySource = None,
    timeout_seconds: int = _SOCIAL_COLLECTION_TIMEOUT_SECONDS,
) -> tuple[SocialData, str | None]:
    if timeout_seconds <= 0:
        try:
            return SocialCollector(api_key=api_key).collect(brand_name, web_content), None
        except Exception as exc:
            return SocialData(brand_name=brand_name, error=str(exc)), "error"

    import sys

    method = "spawn" if sys.platform == "darwin" else ("fork" if "fork" in mp.get_all_start_methods() else "spawn")
    ctx = mp.get_context(method)
    output_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(target=_social_collect_worker, args=(output_queue, api_key, brand_name, web_content))
    process.start()
    process.join(timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join(2)
        if process.is_alive():
            process.kill()
            process.join(2)
        error = f"social_collection_timeout_after_{timeout_seconds}s"
        return SocialData(brand_name=brand_name, error=error), "timeout"

    try:
        status, payload = output_queue.get_nowait()
    except queue.Empty:
        error = "social_collection_no_result"
        return SocialData(brand_name=brand_name, error=error), "error"

    if status == "ok" and isinstance(payload, SocialData):
        return payload, None

    error = str(payload or "social_collection_error")
    return SocialData(brand_name=brand_name, error=error), "error"
