"""Tool aggregation: builtin + MCP + skills behind one provider abstraction.

Every provider returns ``list[Tool]``; the registry merges, dedupes by name and
is fail-soft (one broken provider never takes the rest down). The merged list
is fetched once (lifespan warmup) and cached - MCP tool instances keep their
session alive across turns, and stdio servers are spawned exactly once.

The cached list is what AgentLoop sees, so MCP/skill tools automatically pass
through the policy gate, audit log, tracing and quotas like any builtin tool.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from pi.tools.base import Tool

log = logging.getLogger("pi.tools")


class ToolProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    async def tools(self) -> list[Tool]:
        """Tools this source provides. May raise - the registry swallows it."""

    async def close(self) -> None:
        """Release connections/processes on shutdown."""


class BuiltinToolProvider(ToolProvider):
    name = "builtin"

    async def tools(self) -> list[Tool]:
        from pi.tools import all_tools

        return all_tools()


class ToolRegistry:
    def __init__(self, providers: list[ToolProvider] | None = None):
        self.providers = providers if providers is not None else [BuiltinToolProvider()]
        self._cache: list[Tool] | None = None
        self._lock = asyncio.Lock()

    async def warmup(self) -> None:
        """Preconnect providers (lifespan). Failures are logged, not raised."""
        try:
            await self.tools()
        except Exception:  # noqa: BLE001 - providers are individually fail-soft
            log.exception("tool registry warmup failed")

    async def tools(self) -> list[Tool]:
        """Merged tool list, fetched once and cached. A provider that failed
        during warmup stays absent until the server restarts."""
        if self._cache is not None:
            return self._cache
        async with self._lock:
            if self._cache is not None:  # double-checked under the lock
                return self._cache
            collected: list[Tool] = []
            for provider in self.providers:
                try:
                    collected.extend(await provider.tools())
                except Exception as exc:  # noqa: BLE001 - fail-soft
                    log.warning("tool provider %s failed: %s", provider.name, exc)
            self._cache = _dedupe(collected)
            return self._cache

    def skill_index(self) -> str:
        """Compact skill index for prompt injection; '' when no skill provider."""
        for provider in self.providers:
            index = getattr(provider, "index", None)
            if callable(index):
                return index()
        return ""

    async def close(self) -> None:
        for provider in self.providers:
            try:
                await provider.close()
            except Exception:  # noqa: BLE001 - teardown must not block shutdown
                log.debug("tool provider %s close failed", provider.name, exc_info=True)


def _dedupe(tools: list[Tool]) -> list[Tool]:
    """First provider wins on name collisions; conflicts are logged."""
    seen: dict[str, Tool] = {}
    for tool in tools:
        if tool.name in seen:
            log.warning("duplicate tool name %r ignored", tool.name)
            continue
        seen[tool.name] = tool
    return list(seen.values())
