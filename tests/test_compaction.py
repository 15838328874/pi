"""Compaction tests (migrated from scripts/test_compaction.py)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pi.agent.events import CompactionEvent, ErrorEvent
from pi.agent.loop import AgentLoop
from pi.llm.fake import FakeProvider
from pi.models import TextBlock, ToolCallBlock
from pi.tools import all_tools


def tc(id: str, name: str, args: dict) -> ToolCallBlock:
    return ToolCallBlock(id=id, name=name, arguments=json.dumps(args))


LONG_CONTENT = "x" * 400 + "\n"


class TestCompaction:
    def test_auto_compaction_and_summary_marker(self, tmp_path: Path):
        async def main():
            provider = FakeProvider(
                model="demo",
                responses=[
                    [tc("t1", "write", {"path": "big.txt", "content": LONG_CONTENT})],
                    [TextBlock(text="file written.")],
                    # run 2: compaction consumes this (the summary), then the normal answer
                    [TextBlock(text="SUMMARY: user asked to create big.txt; file was created.")],
                    [TextBlock(text="second turn done.")],
                ],
            )

            agent = AgentLoop(
                provider=provider,
                tools=all_tools(),
                system_prompt="test",
                messages=[],
                cwd=tmp_path,
                compact_threshold=200,  # tiny threshold to force compaction
                compact_keep=2,
            )

            async for _ in agent.run("create big.txt"):
                pass
            size_after_run1 = len(agent.messages)
            assert (tmp_path / "big.txt").read_text(encoding="utf-8") == LONG_CONTENT

            events = []
            async for ev in agent.run("now compact happens before this turn"):
                events.append(ev)

            errors = [ev for ev in events if isinstance(ev, ErrorEvent)]
            assert not errors, [e.message for e in errors]

            compactions = [ev for ev in events if isinstance(ev, CompactionEvent)]
            assert compactions, "expected a CompactionEvent in run 2"
            c = compactions[0]
            assert c.chars_after < c.chars_before

            first = agent.messages[0]
            assert "SUMMARY" in first.blocks[0].text  # type: ignore[index]
            assert "compacted into this summary" in first.blocks[0].text  # type: ignore[index]
            assert len(agent.messages) < size_after_run1 + 4
            return size_after_run1, len(agent.messages)

        size1, size2 = asyncio.run(main())
        assert size1 == 4
        assert size2 == 4  # summary marker + tool result + assistant + user2 + assistant2 kept
