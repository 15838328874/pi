"""Fallback provider: retry with exponential backoff, then degrade the model chain.

Chain config: "openai/qwen3.8-max,openai/qwen3.8-flash,openai/deepseek-v4-flash-0731".
Triggered by transient failures only (connection errors, timeouts, 429/5xx);
argument/schema errors propagate unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from pi.llm.base import LLMProvider, StreamEvent
from pi.models import Message, ToolSpec

log = logging.getLogger("pi.fallback")

FallbackCallback = Callable[[str, str, str], Awaitable[None]]  # (from_model, to_model, reason)

_BACKOFF_BASE = 0.5
_MAX_RETRIES_PER_MODEL = 2


def _is_transient(exc: Exception) -> bool:
    """Transient = worth retrying or degrading on."""
    name = type(exc).__name__
    if name in {
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "RateLimitError",
        "TransportError",
        "ConnectError",
        "ReadTimeout",
        "ConnectTimeout",
    }:
        return True
    import asyncio as _aio

    return isinstance(exc, (TimeoutError, _aio.TimeoutError, ConnectionError))


class FallbackProvider(LLMProvider):
    """Wraps a primary provider with retries and ordered fallback models."""

    name = "fallback"

    def __init__(
        self,
        primary: LLMProvider,
        fallbacks: list[LLMProvider],
        on_fallback: FallbackCallback | None = None,
    ):
        self.chain = [primary] + list(fallbacks)
        self._on_fallback = on_fallback

    @property
    def model(self) -> str:
        return self.chain[0].model

    async def _notify(self, from_model: str, to_model: str, reason: str) -> None:
        if self._on_fallback is not None:
            try:
                await self._on_fallback(from_model, to_model, reason)
            except Exception:  # noqa: BLE001 - notification must never break the run
                log.exception("fallback callback failed")

    async def stream(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> AsyncIterator[StreamEvent]:
        last_exc: Exception | None = None

        for provider in self.chain:
            for attempt in range(1 + _MAX_RETRIES_PER_MODEL):
                got_event = False
                try:
                    async for ev in provider.stream(system, messages, tools):
                        got_event = True
                        yield ev
                    return  # completed cleanly
                except Exception as exc:  # noqa: BLE001
                    if not _is_transient(exc):
                        raise
                    if got_event:
                        # stream already emitted events; retrying would duplicate them
                        raise
                    last_exc = exc
                    if attempt < _MAX_RETRIES_PER_MODEL:
                        delay = _BACKOFF_BASE * (2**attempt)
                        log.warning(
                            "transient error on %s (attempt %d), retrying in %.1fs: %s",
                            provider.model, attempt + 1, delay, exc,
                        )
                        await asyncio.sleep(delay)

            next_index = self.chain.index(provider) + 1
            if next_index < len(self.chain):
                reason = f"{type(last_exc).__name__}: {last_exc}" if last_exc else "unknown"
                await self._notify(provider.model, self.chain[next_index].model, reason)
                log.warning("degrading %s -> %s (%s)", provider.model, self.chain[next_index].model, reason)

        assert last_exc is not None
        raise last_exc
