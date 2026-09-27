"""Anthropic Messages API adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from pi.llm.base import LLMProvider, StreamEnd, StreamEvent, TextDelta, ToolCallDelta
from pi.models import Message, Role, TextBlock, ToolCallBlock, ToolResultBlock, ToolSpec, Usage


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    MAX_TOKENS = 8192

    def __init__(self, model: str, api_key: str | None = None):
        from anthropic import AsyncAnthropic

        # trust_env=False: see OpenAIProvider - ambient proxy env vars must not
        # hijack model traffic. Build the client from whichever httpx the
        # installed anthropic SDK itself uses (this environment's SDK is
        # forked onto httpx2; plain-httpx installs pass httpx.AsyncClient).
        try:
            import httpx2 as _sdk_httpx
        except ImportError:  # pragma: no cover - standard anthropic installs
            _sdk_httpx = httpx
        self.client = AsyncAnthropic(
            api_key=api_key, http_client=_sdk_httpx.AsyncClient(trust_env=False)
        )
        self.model = model

    @staticmethod
    def _to_wire(messages: list[Message]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == Role.user:
                blocks: list[dict[str, Any]] = []
                for b in msg.blocks:
                    if isinstance(b, TextBlock):
                        blocks.append({"type": "text", "text": b.text})
                    elif isinstance(b, ToolResultBlock):
                        blocks.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": b.tool_use_id,
                                "content": b.content,
                                "is_error": b.is_error,
                            }
                        )
                if blocks:
                    wire.append({"role": "user", "content": blocks})
            elif msg.role == Role.assistant:
                blocks = []
                for b in msg.blocks:
                    if isinstance(b, TextBlock):
                        blocks.append({"type": "text", "text": b.text})
                    elif isinstance(b, ToolCallBlock):
                        try:
                            arguments = json.loads(b.arguments or "{}")
                        except json.JSONDecodeError:
                            arguments = {}
                        blocks.append(
                            {"type": "tool_use", "id": b.id, "name": b.name, "input": arguments}
                        )
                if blocks:
                    wire.append({"role": "assistant", "content": blocks})
        return wire

    async def stream(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> AsyncIterator[StreamEvent]:
        from anthropic import NOT_GIVEN

        wire_tools = (
            [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]
            or NOT_GIVEN
        )

        current: dict[int, dict[str, str]] = {}
        async with self.client.messages.stream(
            model=self.model,
            system=system,
            messages=self._to_wire(messages),
            tools=wire_tools,
            max_tokens=self.MAX_TOKENS,
        ) as stream:
            async for ev in stream:
                if ev.type == "content_block_start":
                    cb = ev.content_block
                    if getattr(cb, "type", None) == "tool_use":
                        current[ev.index] = {"id": cb.id, "name": cb.name, "json": ""}
                elif ev.type == "content_block_delta":
                    if ev.delta.type == "text_delta":
                        yield TextDelta(ev.delta.text)
                    elif ev.delta.type == "input_json_delta":
                        if ev.index in current:
                            current[ev.index]["json"] += ev.delta.partial_json
            final = await stream.get_final_message()

        for index in sorted(current):
            c = current[index]
            arguments = c["json"] or "{}"
            try:
                json.loads(arguments)
            except json.JSONDecodeError:
                arguments = "{}"
            yield ToolCallDelta(id=c["id"], name=c["name"], arguments=arguments)

        stop = "tool_use" if final.stop_reason == "tool_use" else "end_turn"
        usage = Usage(
            input_tokens=final.usage.input_tokens,
            output_tokens=final.usage.output_tokens,
        )
        yield StreamEnd(stop, usage)
