"""MCP tool tests against a fake stdio server (tests/fake_mcp_server.py).

No network: the fake server is a child process speaking JSON-RPC per line.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from pi.tools.base import ToolContext
from pi.tools.mcp import McpToolProvider

FAKE = str(Path(__file__).parent / "fake_mcp_server.py")


def _cmd(**env):
    return [sys.executable, FAKE], env


def test_lists_tools_from_stdio_server(tmp_path):
    prov = McpToolProvider([{"name": "fake", "command": [sys.executable, FAKE]}])

    async def main():
        tools = {t.name: t for t in await prov.tools()}
        assert set(tools) == {"echo", "fail"}
        assert tools["echo"].description
        assert tools["echo"].input_schema["properties"]["text"] == {"type": "string"}
        ctx = ToolContext(cwd=tmp_path)
        r = await tools["echo"].execute({"text": "hi"}, ctx)
        assert not r.is_error
        assert "echo: hi" in r.content
        await prov.close()

    asyncio.run(main())


def test_is_error_maps_to_tool_result(tmp_path):
    prov = McpToolProvider([{"name": "fake", "command": [sys.executable, FAKE]}])

    async def main():
        tools = {t.name: t for t in await prov.tools()}
        r = await tools["fail"].execute({}, ToolContext(cwd=tmp_path))
        assert r.is_error
        assert "boom" in r.content
        await prov.close()

    asyncio.run(main())


def test_unreachable_server_is_fail_soft():
    prov = McpToolProvider(
        [{"name": "bad", "command": [sys.executable, "/nonexistent/server.py"]}]
    )

    async def main():
        tools = await prov.tools()
        assert tools == []  # no raise, logged
        await prov.close()

    asyncio.run(main())


def test_dead_server_yields_tool_error(tmp_path):
    """Server exits after tools/list: the cached tool still exists, its calls error."""
    prov = McpToolProvider(
        [{"name": "fake", "command": [sys.executable, FAKE], "env": {"FAKE_MCP_BEHAVIOR": "exit_after_list"}}]
    )

    async def main():
        tools = await prov.tools()
        assert [t.name for t in tools] == ["echo", "fail"]
        r = await tools[0].execute({"text": "x"}, ToolContext(cwd=tmp_path))
        assert r.is_error  # connection is dead -> surfaced as a tool error
        await prov.close()

    asyncio.run(main())


def test_close_terminates_stdio_server():
    prov = McpToolProvider([{"name": "fake", "command": [sys.executable, FAKE]}])

    async def main():
        await prov.tools()
        # same event loop as the connect: the SDK's cancel scopes are loop-bound
        await prov.close()

    asyncio.run(main())  # must not raise/hang (child process terminated)
