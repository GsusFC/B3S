"""Thread-safe API-key rotation without exposing credential values."""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable


ApiKeySource = str | Iterable[str] | None


def normalize_api_keys(*sources: ApiKeySource) -> tuple[str, ...]:
    """Normalize comma/newline-delimited or iterable key sources and deduplicate them."""
    keys: list[str] = []
    seen: set[str] = set()

    def add(source: ApiKeySource) -> None:
        if source is None:
            return
        if isinstance(source, str):
            value = source.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1].strip()
            for item in re.split(r"[,\n]", value):
                key = item.strip().strip('"\'')
                if key and key not in seen:
                    seen.add(key)
                    keys.append(key)
            return
        for item in source:
            add(item)

    for source in sources:
        add(source)
    return tuple(keys)


class ApiKeyPool:
    """Round-robin key selector safe for concurrent scanner workers."""

    def __init__(self, keys: ApiKeySource):
        self.__keys = normalize_api_keys(keys)
        self.__index = 0
        self.__lock = threading.Lock()

    @property
    def size(self) -> int:
        return len(self.__keys)

    @property
    def configured(self) -> bool:
        return bool(self.__keys)

    def next_key(self) -> str:
        if not self.__keys:
            return ""
        with self.__lock:
            key = self.__keys[self.__index]
            self.__index = (self.__index + 1) % len(self.__keys)
            return key

    def __bool__(self) -> bool:
        return self.configured

    def __repr__(self) -> str:
        return f"ApiKeyPool(size={self.size})"


_SHARED_POOLS: dict[tuple[str, tuple[str, ...]], ApiKeyPool] = {}
_SHARED_POOLS_LOCK = threading.Lock()


def shared_api_key_pool(provider: str, keys: ApiKeySource) -> ApiKeyPool:
    """Reuse a provider pool so independent scans distribute load globally."""
    normalized = normalize_api_keys(keys)
    registry_key = (provider.strip().lower(), normalized)
    with _SHARED_POOLS_LOCK:
        pool = _SHARED_POOLS.get(registry_key)
        if pool is None:
            pool = ApiKeyPool(normalized)
            _SHARED_POOLS[registry_key] = pool
        return pool
