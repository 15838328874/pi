"""Per-user fixed-window rate limiting.

Backed by the shared CacheBackend: Redis when PI_REDIS_URL is set (correct
across instances), process memory otherwise.
"""

from __future__ import annotations

from pi.server.cache import CacheBackend, MemoryBackend


class RateLimiter:
    def __init__(self, max_per_window: int, backend: CacheBackend | None = None, window_seconds: float = 60.0):
        self.max_per_window = max_per_window
        self.window_seconds = window_seconds
        self.backend = backend or MemoryBackend()

    async def allow(self, key: str) -> bool:
        count = await self.backend.incr_window(key, self.window_seconds)
        return count <= self.max_per_window

    def retry_after(self, key: str) -> float:
        """Conservative upper bound until the window resets."""
        return self.window_seconds
