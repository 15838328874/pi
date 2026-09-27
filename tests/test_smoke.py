"""End-to-end smoke test using FakeProvider (no API key needed).

Covers: direct tool execution, and a full agent loop driving four tool calls
whose messages are checked through the on_message callback.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pi.agent.events import (
    ErrorEvent,
    TextDeltaEvent,
    ToolCallEndEvent,
    TurnEndEvent,
)
from pi.agent.loop import AgentLoop
from pi.llm.fake import FakeProvider
from pi.models import Message, Role, TextBlock, ToolCallBlock
from pi.prompt import SYSTEM_PROMPT
from pi.tools import all_tools
from pi.tools.base import ToolContext


def tc(id: str, name: str, args: dict) -> ToolCallBlock:
    return ToolCallBlock(id=id, name=name, arguments=json.dumps(args))


class TestDirectTools:
    def test_write_ls_find_grep_bash(self, tmp_path: Path):
        async def main():
            ctx = ToolContext(cwd=tmp_path)
            tools = {t.name: t for t in all_tools()}

            r = await tools["write"].execute(
                {
                    "path": "src/app.py",
                    "content": "def hello():\n    return 'hello pi'\n\ndef bye():\n    return 'bye'\n",
                },
                ctx,
            )
            assert not r.is_error, r.content
            r = await tools["ls"].execute({"path": "."}, ctx)
            assert "src/" in r.content, r.content
            r = await tools["find"].execute({"pattern": "*.py"}, ctx)
            assert "src/app.py" in r.content, r.content
            r = await tools["grep"].execute({"pattern": "hello", "include": "*.py"}, ctx)
            assert "src/app.py:1:" in r.content, r.content
            r = await tools["bash"].execute({"command": "echo smoke-bash-ok"}, ctx)
            assert "smoke-bash-ok" in r.content and "(exit code: 0)" in r.content, r.content

        asyncio.run(main())


class TestAgentLoopE2E:
    def test_full_tool_flow_and_message_callback(self, tmp_path: Path):
        async def main():
            saved: list[Message] = []

            def on_message(m: Message) -> None:
                saved.append(m)

            provider = FakeProvider(
                model="demo",
                responses=[
                    [tc("t1", "write", {"path": "notes.txt", "content": "hello pi\n"})],
                    [tc("t2", "read", {"path": "notes.txt"})],
                    [
                        tc(
                            "t3",
                            "edit",
                            {"path": "notes.txt", "old_string": "hello", "new_string": "Hello"},
                        )
                    ],
                    [tc("t4", "grep", {"pattern": "Hello", "include": "*.txt"})],
                    [TextBlock(text="all four tool steps completed successfully.")],
                ],
            )

            agent = AgentLoop(
                provider=provider,
                tools=all_tools(),
                system_prompt=SYSTEM_PROMPT,
                messages=[],
                cwd=tmp_path,
                on_message=on_message,
            )

            events: list = []
            async for ev in agent.run("run the smoke steps"):
                events.append(ev)

            assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "Hello pi\n"
            assert not any(isinstance(ev, ErrorEvent) for ev in events), [
                ev.message for ev in events if isinstance(ev, ErrorEvent)
            ]
            assert any(isinstance(ev, TurnEndEvent) for ev in events)
            full_text = "".join(ev.text for ev in events if isinstance(ev, TextDeltaEvent))
            assert "successfully" in full_text, full_text[-200:]
            tool_ends = [ev for ev in events if isinstance(ev, ToolCallEndEvent)]
            assert all(e.ok for e in tool_ends), tool_ends

            roles = [m.role for m in saved]
            assert roles[0] == Role.user
            assert any(
                m.role == Role.assistant and any(isinstance(b, ToolCallBlock) for b in m.blocks)
                for m in saved
            )
            assert saved[-1].role == Role.assistant
            assert isinstance(saved[-1].blocks[0], TextBlock)
            return len(saved)

        n = asyncio.run(main())
        assert n >= 10  # user + 4x(assistant toolcall + toolresult) + final assistant
