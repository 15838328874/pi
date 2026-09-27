"""OpenAI Chat Completions adapter (works for any OpenAI-compatible endpoint)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from pi.llm.base import LLMProvider, StreamEnd, StreamEvent, TextDelta, ToolCallDelta
from pi.llm.think_filter import ThinkFilter
from pi.models import Message, Role, TextBlock, ToolCallBlock, ToolResultBlock, ToolSpec, Usage


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None):
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
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
        return wire

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
                usage = Usage(
                    input_tokens=chunk.usage.prompt_tokens or 0,
                    output_tokens=chunk.usage.completion_tokens or 0,
                )
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta and delta.content:
                visible = think.feed(delta.content)
                if visible:
                    yield TextDelta(visible)
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

        tail = think.flush()
        if tail:
            yield TextDelta(tail)

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
