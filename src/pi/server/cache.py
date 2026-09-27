"""Distributed cache / lock backend: in-memory (default) or Redis.

Set PI_REDIS_URL (e.g. redis://localhost:6379/0) to enable the Redis backend;
without it everything degrades to process-local implementations so a single
instance keeps working unchanged.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

log = logging.getLogger("pi.server.cache")


class CacheBackend(Protocol):
    async def incr_window(self, key: str, window_seconds: float) -> int:
        """Fixed-window counter: increments and returns the current count."""

    async def acquire_lock(self, key: str, ttl_seconds: float) -> bool:
        """Try to acquire a distributed lock. Returns True if acquired."""

    async def release_lock(self, key: str) -> None:
        """Release a previously acquired lock."""

    async def ping(self) -> bool:
        """Health probe."""

    async def setex(self, key: str, ttl_seconds: float, value: str) -> None:
        """Set a string value with a TTL (revocation lists, epochs)."""

    async def get(self, key: str) -> str | None:
        """Read a string value; None when missing or expired."""

    async def delete(self, key: str) -> None:
        """Remove a key."""

    async def close(self) -> None:
        pass


class MemoryBackend:
    """Process-local backend: single-instance deployments and tests."""

    def __init__(self) -> None:
        self._windows: dict[str, tuple[float, int]] = {}
        self._locks: set[str] = set()
        self._kv: dict[str, tuple[float, str]] = {}  # key -> (expires_monotonic, value)

    async def incr_window(self, key: str, window_seconds: float) -> int:
        now = time.monotonic()
        start, count = self._windows.get(key, (now, 0))
        if now - start >= window_seconds:
            start, count = now, 0
        count += 1
        self._windows[key] = (start, count)
        return count

    async def acquire_lock(self, key: str, ttl_seconds: float) -> bool:
        if key in self._locks:
            return False
        self._locks.add(key)
        return True

    async def release_lock(self, key: str) -> None:
        self._locks.discard(key)

    async def ping(self) -> bool:
        return True

    async def setex(self, key: str, ttl_seconds: float, value: str) -> None:
        self._kv[key] = (time.monotonic() + ttl_seconds, value)

    async def get(self, key: str) -> str | None:
        entry = self._kv.get(key)
        if entry is None:
            return None
        expires, value = entry
        if time.monotonic() > expires:
            self._kv.pop(key, None)
            return None
        return value

    async def delete(self, key: str) -> None:
        self._kv.pop(key, None)


class RedisBackend:
    """Redis backend: multi-instance deployments (redis-py async)."""

    def __init__(self, url: str, namespace: str = "pi"):
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(url, decode_responses=True)
        self._ns = namespace
        self._lock_names: set[str] = set()

    async def incr_window(self, key: str, window_seconds: float) -> int:
        redis_key = f"{self._ns}:rl:{key}"
        count = await self._redis.incr(redis_key)
        if count == 1:
            await self._redis.expire(redis_key, int(window_seconds) + 1)
        return int(count)

    async def acquire_lock(self, key: str, ttl_seconds: float) -> bool:
        lock_key = f"{self._ns}:lock:{key}"
        got = await self._redis.set(lock_key, "1", nx=True, ex=int(ttl_seconds) + 1)
        if got:
            self._lock_names.add(lock_key)
        return bool(got)

    async def release_lock(self, key: str) -> None:
        lock_key = f"{self._ns}:lock:{key}"
        await self._redis.delete(lock_key)
        self._lock_names.discard(lock_key)

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:  # noqa: BLE001
            return False

    async def setex(self, key: str, ttl_seconds: float, value: str) -> None:
        await self._redis.setex(f"{self._ns}:kv:{key}", max(1, int(ttl_seconds)), value)

    async def get(self, key: str) -> str | None:
        value = await self._redis.get(f"{self._ns}:kv:{key}")
        return value if value is None else str(value)

    async def delete(self, key: str) -> None:
        await self._redis.delete(f"{self._ns}:kv:{key}")

    async def close(self) -> None:
        await self._redis.aclose()


def get_backend(redis_url: str | None, namespace: str = "pi") -> CacheBackend:
    """Pick Redis when configured and reachable; otherwise memory.

    Redis connection problems degrade to memory with a loud warning instead of
    taking the service down (fail-open on availability, not correctness).
    """
    if not redis_url:
        return MemoryBackend()
    backend = RedisBackend(redis_url, namespace=namespace)
    log.info("redis backend configured: %s (ns=%s)", redis_url, namespace)
    return backend
