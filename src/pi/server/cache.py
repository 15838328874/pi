"""Distributed cache / lock backend: in-memory (default) or Redis.

Set PI_REDIS_URL (e.g. redis://localhost:6379/0) to enable the Redis backend;
without it everything degrades to process-local implementations so a single
instance keeps working unchanged.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Protocol

log = logging.getLogger("pi.server.cache")


class CacheBackend(Protocol):
    async def incr_window(self, key: str, window_seconds: float) -> int:
        """Fixed-window counter: increments and returns the current count."""

    async def acquire_lock(self, key: str, ttl_seconds: float) -> bool:
        """Try to acquire a distributed lock. Returns True if acquired."""

    async def release_lock(self, key: str) -> None:
        """Release a previously acquired lock."""

    async def has_lock(self, key: str) -> bool:
        """Whether a lock for ``key`` is currently held (for UI running-state)."""

    async def acquire_lock_owned(self, key: str, ttl_seconds: float) -> str | None:
        """Acquire a lock and return a unique ownership token, or None if not
        acquired. The token must be passed to ``release_lock_owned`` so a holder
        whose TTL has expired (and been re-acquired by someone else) cannot
        delete the new holder's lock. Compare-and-delete release semantics."""

    async def release_lock_owned(self, key: str, token: str) -> bool:
        """Release ONLY if still owned (token matches). Returns True if released.
        False means the lock was already expired / taken by someone else."""

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
        self._owned_locks: dict[str, str] = {}  # key -> ownership token
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

    async def has_lock(self, key: str) -> bool:
        return key in self._locks

    async def acquire_lock_owned(self, key: str, ttl_seconds: float) -> str | None:
        if key in self._owned_locks:
            return None
        token = uuid.uuid4().hex
        self._owned_locks[key] = token
        return token

    async def release_lock_owned(self, key: str, token: str) -> bool:
        if self._owned_locks.get(key) != token:
            return False  # expired / re-acquired by someone else: do not delete
        del self._owned_locks[key]
        return True

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

    async def has_lock(self, key: str) -> bool:
        return bool(await self._redis.exists(f"{self._ns}:lock:{key}"))

    async def acquire_lock_owned(self, key: str, ttl_seconds: float) -> str | None:
        lock_key = f"{self._ns}:lock:{key}"
        token = uuid.uuid4().hex
        got = await self._redis.set(lock_key, token, nx=True, ex=int(ttl_seconds) + 1)
        return token if got else None

    async def release_lock_owned(self, key: str, token: str) -> bool:
        lock_key = f"{self._ns}:lock:{key}"
        # Atomic compare-and-delete: only the current holder's token may delete.
        # A bare GET-then-DEL has a race (the lock can expire and be re-acquired
        # between the two round-trips), so this runs server-side as one script.
        deleted = await self._redis.eval(
            "if redis.call('GET', KEYS[1]) == ARGV[1] "
            "then return redis.call('DEL', KEYS[1]) else return 0 end",
            1,
            lock_key,
            token,
        )
        return bool(deleted)

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
    # mask_url: the Redis URL's userinfo is the plaintext password - the
    # startup log lands in journald and must not carry it (L15).
    from pi.security.redact import mask_url

    log.info("redis backend configured: %s (ns=%s)", mask_url(redis_url), namespace)
    return backend
