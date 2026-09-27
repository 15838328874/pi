"""Scripted provider for tests and key-less demos."""

from __future__ import annotations

from collections.abc import AsyncIterator

from pi.llm.base import LLMProvider, StreamEnd, StreamEvent, TextDelta, ToolCallDelta
from pi.models import Message, TextBlock, ToolCallBlock, ToolSpec, Usage

DEMO_TEXT = (
    "你好！我是 pi-py 的 FakeProvider 演示模式（model = fake/demo），未使用任何 API key。"
    "切换真实模型请设置环境变量 PI_MODEL=openai/<model> 或 anthropic/<model>。"
)


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(self, model: str = "demo", responses: list[list[object]] | None = None):
        self.model = model or "demo"
        self.responses: list[list[object]] = list(responses or [])

    async def stream(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> AsyncIterator[StreamEvent]:
        if self.responses:
            blocks = self.responses.pop(0)
        else:
            blocks = [TextBlock(text=DEMO_TEXT)]
        for b in blocks:
            if isinstance(b, TextBlock):
                text = b.text
                for i in range(0, len(text), 16):
                    yield TextDelta(text[i : i + 16])
            elif isinstance(b, ToolCallBlock):
                yield ToolCallDelta(id=b.id, name=b.name, arguments=b.arguments)
        has_tools = any(isinstance(b, ToolCallBlock) for b in blocks)
        yield StreamEnd(
            "tool_use" if has_tools else "end_turn",
            Usage(input_tokens=1, output_tokens=1),
        )
