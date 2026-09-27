"""Tests for recursive sub-agent delegation (spawn_subagents tool)."""

from __future__ import annotations

import asyncio
import json

from pi.llm.base import LLMProvider, StreamEnd, TextDelta
from pi.llm.fake import FakeProvider
from pi.models import TextBlock, ToolCallBlock, Usage
from pi.tools import all_tools
from pi.tools.base import ToolContext
from pi.tools.subagent import SpawnSubagentsTool


def test_depth_limit_blocks_further_delegation():
    tool = SpawnSubagentsTool(depth=3, max_depth=3)

    async def main():
        ctx = ToolContext()
        result = await tool.execute({"tasks": [{"task": "do it"}]}, ctx)
        assert result.is_error
        assert "depth limit" in result.content

    asyncio.run(main())


def test_single_subagent_writes_file_and_aggregates(tmp_path):
    provider = FakeProvider(
        model="demo",
        responses=[
            [
                ToolCallBlock(
                    id="t1",
                    name="write",
                    arguments=json.dumps(
                        {"path": "out.txt", "content": "hello from subagent"}
                    ),
                )
            ],
            [TextBlock(text="wrote the file")],
        ],
    )
    tool = SpawnSubagentsTool(depth=0, max_depth=3)

    async def main():
        ctx = ToolContext(cwd=tmp_path)
        ctx.provider = provider
        result = await tool.execute({"tasks": [{"task": "write out.txt"}]}, ctx)
        assert not result.is_error
        assert "OK" in result.content
        assert "wrote the file" in result.content
        # sub-agent shares the parent workspace by default, so its write is visible
        assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello from subagent"
        # the child ran two turns, each StreamEnd carrying 1 output token
        assert result.usage is not None
        assert result.usage.output_tokens == 2

    asyncio.run(main())


class _StatelessProvider(LLMProvider):
    """Returns text, or raises when the incoming prompt asks to fail."""

    name = "fake"
    model = "test"

    async def stream(self, system, messages, tools):
        joined = ""
        for m in messages:
            for b in m.blocks:
                text = getattr(b, "text", None)
                if text:
                    joined += text
        if "FAIL" in joined:
            raise RuntimeError("boom")
        text = "result-ok"
        for i in range(0, len(text), 8):
            yield TextDelta(text[i : i + 8])
        yield StreamEnd("end_turn", Usage(input_tokens=1, output_tokens=1))


def test_parallel_subagents_and_error_isolation(tmp_path):
    tool = SpawnSubagentsTool(depth=0, max_depth=3)

    async def main():
        ctx = ToolContext(cwd=tmp_path)
        ctx.provider = _StatelessProvider()
        result = await tool.execute(
            {
                "tasks": [
                    {"task": "task-A", "isolated": True},
                    {"task": "task-B", "isolated": True},
                    {"task": "task-FAIL-me", "isolated": True},
                ]
            },
            ctx,
        )
        # the batch call itself succeeds; per-task failures are inline
        assert not result.is_error
        assert "OK" in result.content
        assert "ERROR" in result.content
        assert "RuntimeError" in result.content
        # each isolated task got its own sub-directory (no concurrent-write clash)
        subdirs = [p for p in tmp_path.iterdir() if p.name.startswith(".subagent_")]
        assert len(subdirs) == 3
        # only the two OK children contributed output tokens (the failing one
        # raised before yielding StreamEnd)
        assert result.usage is not None
        assert result.usage.output_tokens == 2

    asyncio.run(main())


def test_all_tools_threads_subagent_depth():
    tool = next(
        t
        for t in all_tools(subagent_depth=2, max_subagent_depth=5)
        if t.name == "spawn_subagents"
    )
    assert tool.depth == 2
    assert tool.max_depth == 5
