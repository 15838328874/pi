"""ToolRegistry tests: aggregation, dedupe, fail-soft, caching, skill index."""

from __future__ import annotations

import asyncio

from pi.tools.base import Tool, ToolResult
from pi.tools.registry import ToolRegistry, ToolProvider


class _StubTool(Tool):
    def __init__(self, name: str):
        self.name = name
        self.description = ""
        self.input_schema = {}

    async def execute(self, args, ctx):
        return ToolResult(content="ok")


class _Provider(ToolProvider):
    def __init__(self, name, tools=None, fail=False):
        self.name = name
        self._tools = tools or []
        self.fail = fail
        self.calls = 0
        self.closed = False

    async def tools(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        return list(self._tools)

    async def close(self):
        self.closed = True


def _run(coro):
    return asyncio.run(coro)


def test_aggregates_providers():
    reg = ToolRegistry(
        [_Provider("a", [_StubTool("t1")]), _Provider("b", [_StubTool("t2")])]
    )
    tools = _run(reg.tools())
    assert [t.name for t in tools] == ["t1", "t2"]


def test_dedupe_first_wins():
    reg = ToolRegistry(
        [_Provider("a", [_StubTool("dup")]), _Provider("b", [_StubTool("dup"), _StubTool("other")])]
    )
    tools = _run(reg.tools())
    assert [t.name for t in tools] == ["dup", "other"]


def test_fail_soft_skips_broken_provider():
    reg = ToolRegistry(
        [_Provider("bad", fail=True), _Provider("good", [_StubTool("ok")])]
    )
    tools = _run(reg.tools())
    assert [t.name for t in tools] == ["ok"]


def test_tools_fetched_once_and_cached():
    a = _Provider("a", [_StubTool("t1")])
    reg = ToolRegistry([a])
    _run(reg.tools())
    _run(reg.tools())
    assert a.calls == 1


def test_close_calls_provider_close():
    a = _Provider("a", [_StubTool("t1")])
    reg = ToolRegistry([a])
    _run(reg.close())
    assert a.closed


def test_default_registry_is_builtin_only():
    reg = ToolRegistry()
    tools = _run(reg.tools())
    names = {t.name for t in tools}
    assert {"bash", "read", "write", "remember", "recall"} <= names


def test_skill_index_passthrough():
    class _SkillProvider(_Provider):
        def index(self):
            return "- code-review: 审查代码"

    reg = ToolRegistry([_SkillProvider("skills")])
    assert reg.skill_index() == "- code-review: 审查代码"


def test_skill_index_empty_without_skills():
    assert ToolRegistry().skill_index() == ""
