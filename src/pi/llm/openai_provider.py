"""OpenAI Chat Completions adapter (works for any OpenAI-compatible endpoint)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from pi.llm.base import LLMProvider, StreamEnd, StreamEvent, TextDelta, ToolCallDelta
from pi.llm.think_filter import ThinkFilter
from pi.models import FileBlock, Message, Role, TextBlock, ToolCallBlock, ToolResultBlock, ToolSpec, Usage

#: gateway-executed tool types the OpenAI-compatible endpoint understands as
#: bare `{"type": ...}` entries in the tools array. The model answers them with
#: a normal function tool_call; the client returns an empty tool result and the
#: gateway performs the actual search/extraction/execution.
BUILTIN_TOOL_TYPES = ("web_search", "web_extractor", "code_interpreter")


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        enable_search: bool = False,
        builtin_tools: list[str] | None = None,
    ):
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.enable_search = enable_search
        self.builtin_tools = [t for t in (builtin_tools or []) if t in BUILTIN_TOOL_TYPES]

    @staticmethod
    def _to_wire(system: str, messages: list[Message]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for msg in messages:
            if msg.role == Role.user:
                files = [b for b in msg.blocks if isinstance(b, FileBlock)]
                if files:
                    # file blocks demand the content-array form: one file entry
                    # per block, then one text entry per (joined) text run.
                    content: list[dict[str, Any]] = [
                        {"type": "file", "file": {"file_url": f.file_url}} for f in files
                    ]
                    text = "\n".join(b.text for b in msg.blocks if isinstance(b, TextBlock))
                    if text:
                        content.append({"type": "text", "text": text})
                    wire.append({"role": "user", "content": content})
                else:
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
        wire_tools: list[dict[str, Any]] = [
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
        wire_tools.extend({"type": t} for t in self.builtin_tools)
        # enable_search alone is advisory: the gateway skips the search unless
        # the model itself wants one, so a user-toggled 联网搜索 silently
        # produced "I can't provide live data" answers. forced_search makes
        # the toggle deterministic - probed 2026-09-05, all qwen models.
        extra_body: dict[str, Any] | None = (
            {"enable_search": True, "search_options": {"forced_search": True}}
            if self.enable_search
            else None
        )
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=self._to_wire(system, messages),
            tools=wire_tools or None,
            stream=True,
            stream_options={"include_usage": True},
            extra_body=extra_body,
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
