"""OpenAI Chat Completions adapter (works for any OpenAI-compatible endpoint)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from pi.llm.base import LLMProvider, StreamEnd, StreamEvent, TextDelta, ThinkingDelta, ToolCallDelta
from pi.llm.think_filter import ThinkFilter
from pi.models import Message, Role, TextBlock, ToolCallBlock, ToolResultBlock, ToolSpec, Usage


def _cache_tokens(u: Any) -> tuple[int, int]:
    """Normalize provider prompt-cache counters to (hit, miss). Best-effort.

    Two shapes exist in the wild and we accept both, because the same code paths
    serve DeepSeek and OpenAI-compatible endpoints:
      - DeepSeek (and several Chinese providers): flat fields on usage, which the
        OpenAI SDK parks in ``model_extra`` (they are not in its typed schema);
      - OpenAI: nested ``prompt_tokens_details.cached_tokens`` (hit only - miss
        is then whatever is left of ``prompt_tokens``).
    Never raises and never guesses: unknown shape -> (0, 0), which reads as
    "cache not observed" rather than a fake hit rate.
    """
    extra = getattr(u, "model_extra", None) or {}
    hit = getattr(u, "prompt_cache_hit_tokens", None)
    if hit is None:
        hit = extra.get("prompt_cache_hit_tokens")
    miss = getattr(u, "prompt_cache_miss_tokens", None)
    if miss is None:
        miss = extra.get("prompt_cache_miss_tokens")
    if hit is None or miss is None:
        details = getattr(u, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", None) if details is not None else None
        if cached is not None:
            hit = cached
            if miss is None:
                total = getattr(u, "prompt_tokens", 0) or 0
                miss = max(0, total - int(cached))
    return int(hit or 0), int(miss or 0)


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None):
        from openai import AsyncOpenAI

        # trust_env=False: ambient proxy env vars (a dead local proxy, a SOCKS
        # proxy without socksio, ...) must not silently hijack model traffic -
        # the endpoint in base_url is always an explicit, direct destination.
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=httpx.AsyncClient(trust_env=False),
        )
        self.model = model

    @staticmethod
    def _to_wire(system: str, messages: list[Message]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for msg in messages:
            if msg.role == Role.user:
                text = "\n".join(b.text for b in msg.blocks if isinstance(b, TextBlock))
                if text:
                    wire.append({"role": "user", "content": text})
                for b in msg.blocks:
                    if isinstance(b, ToolResultBlock):
                        wire.append(
                            {
                                "role": "tool",
                                "tool_call_id": b.tool_use_id,
                                "content": b.content,
                            }
                        )
            elif msg.role == Role.assistant:
                text = "\n".join(b.text for b in msg.blocks if isinstance(b, TextBlock))
                entry: dict[str, Any] = {"role": "assistant", "content": text or None}
                calls = [b for b in msg.blocks if isinstance(b, ToolCallBlock)]
                if calls:
                    entry["tool_calls"] = [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": c.arguments},
                        }
                        for c in calls
                    ]
                wire.append(entry)
        return OpenAIProvider._repair_tool_sequence(wire)

    @staticmethod
    def _repair_tool_sequence(wire: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """补全残缺序列：assistant 带 tool_calls 但后面缺对应 tool 消息时，
        补一个空 tool 消息兜底，避免下轮发给 LLM 触发 400
        "insufficient tool messages following tool_calls message"。
        残缺序列来自 run 中断/超时——assistant 已落库、tool 结果未落库。"""
        out: list[dict[str, Any]] = []
        pending: list[str] = []
        for entry in wire:
            role = entry.get("role")
            if role == "tool":
                out.append(entry)
                tid = entry.get("tool_call_id")
                if tid in pending:
                    pending.remove(tid)
            elif role == "assistant" and entry.get("tool_calls"):
                for cid in pending:
                    out.append({"role": "tool", "tool_call_id": cid, "content": ""})
                pending = [c["id"] for c in entry["tool_calls"]]
                out.append(entry)
            else:
                for cid in pending:
                    out.append({"role": "tool", "tool_call_id": cid, "content": ""})
                pending = []
                out.append(entry)
        for cid in pending:
            out.append({"role": "tool", "tool_call_id": cid, "content": ""})
        return out

    async def stream(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> AsyncIterator[StreamEvent]:
        wire_tools = (
            [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
            or None
        )
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=self._to_wire(system, messages),
            tools=wire_tools,
            stream=True,
            stream_options={"include_usage": True},
        )

        calls: dict[int, dict[str, str]] = {}
        finish: str | None = None
        usage = Usage()
        think = ThinkFilter()

        async for chunk in stream:
            if chunk.usage:
                hit, miss = _cache_tokens(chunk.usage)
                usage = Usage(
                    input_tokens=chunk.usage.prompt_tokens or 0,
                    output_tokens=chunk.usage.completion_tokens or 0,
                    cache_hit_tokens=hit,
                    cache_miss_tokens=miss,
                )
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta and delta.content:
                visible, thinking = think.feed(delta.content)
                if thinking:
                    yield ThinkingDelta(thinking)
                if visible:
                    yield TextDelta(visible)
            # 推理模型（deepseek/qwen）把思考放在独立的 reasoning_content 字段，
            # 与 content 分开流式；这里同样透传为 ThinkingDelta。
            if delta and getattr(delta, "reasoning_content", None):
                yield ThinkingDelta(delta.reasoning_content)
            if delta and delta.tool_calls:
                for tc in delta.tool_calls:
                    slot = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] += tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments
            if choice.finish_reason:
                finish = choice.finish_reason

        tail_visible, tail_think = think.flush()
        if tail_think:
            yield ThinkingDelta(tail_think)
        if tail_visible:
            yield TextDelta(tail_visible)

        for index in sorted(calls):
            c = calls[index]
            arguments = c["arguments"] or "{}"
            try:
                json.loads(arguments)  # sanity check; pass through regardless
            except json.JSONDecodeError:
                arguments = "{}"
            yield ToolCallDelta(id=c["id"] or f"call_{index}", name=c["name"], arguments=arguments)

        stop = "tool_use" if finish == "tool_calls" else "end_turn"
        yield StreamEnd(stop, usage)
