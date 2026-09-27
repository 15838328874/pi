"""Skills tests: loading, index, use_skill, script tools, prompt injection."""

from __future__ import annotations

import asyncio
from pathlib import Path

from conftest import TEST_DB_URL

from pi.llm.fake import FakeProvider
from pi.models import Role, TextBlock
from pi.security.policy import Policy
from pi.server.db import Database, MessageRepo, SessionRow
from pi.server.runner import RunManager
from pi.tools.base import ToolContext
from pi.tools.registry import ToolRegistry
from pi.tools.skill import SkillToolProvider


def _make_skill_dir(root: Path) -> Path:
    skill = root / "code-review"
    skill.mkdir(parents=True)
    (skill / "scripts").mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: code-review\ndescription: 对改动做代码审查\n---\n"
        "1. 先看 diff\n2. 输出问题清单\n",
        encoding="utf-8",
    )
    (skill / "scripts" / "check.py").write_text(
        "import sys\n"
        "with open('args.txt', 'a') as f:\n"
        "    f.write('|'.join(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    # malformed skill: no SKILL.md -> skipped, must not break the rest
    (root / "broken").mkdir()
    (root / "broken" / "other.txt").write_text("nope", encoding="utf-8")
    return root


def test_load_index_and_tools(tmp_path):
    prov = SkillToolProvider([_make_skill_dir(tmp_path / "skills")])
    assert "code_review" in prov.skills
    assert "broken" not in prov.skills
    assert prov.index() == "- code_review: 对改动做代码审查"

    async def main():
        tools = {t.name: t for t in await prov.tools()}
        assert "use_skill" in tools
        assert "skill_code_review_check" in tools
        # use_skill returns the body (progressive loading)
        r = await tools["use_skill"].execute({"skill": "code_review"}, ToolContext(cwd=tmp_path))
        assert "输出问题清单" in r.content
        # unknown skill
        r = await tools["use_skill"].execute({"skill": "nope"}, ToolContext(cwd=tmp_path))
        assert r.is_error

    asyncio.run(main())


def test_script_tool_runs_with_args_in_workspace(tmp_path):
    prov = SkillToolProvider([_make_skill_dir(tmp_path / "skills")])
    ws = tmp_path / "ws"
    ws.mkdir()

    async def main():
        tool = next(t for t in await prov.tools() if t.name == "skill_code_review_check")
        r = await tool.execute({"args": "--strict --verbose"}, ToolContext(cwd=ws))
        assert not r.is_error, r.content
        assert "(exit code: 0)" in r.content
        # the script ran with the workspace as cwd and got the args verbatim
        assert (ws / "args.txt").read_text(encoding="utf-8").strip() == "--strict|--verbose"
        # script was staged under .pi-skills so the sandbox (workspace mount) sees it
        assert (ws / ".pi-skills" / "code_review" / "check.py").is_file()

    asyncio.run(main())


def test_script_tool_failure_is_error(tmp_path):
    root = tmp_path / "skills"
    skill = root / "failing"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: failing\ndescription: x\n---\nbody", encoding="utf-8")
    (skill / "scripts" / "boom.py").write_text("import sys; sys.exit(3)\n", encoding="utf-8")
    prov = SkillToolProvider([root])
    ws = tmp_path / "ws"
    ws.mkdir()

    async def main():
        tool = next(t for t in await prov.tools() if t.name == "skill_failing_boom")
        r = await tool.execute({}, ToolContext(cwd=ws))
        assert "(exit code: 3)" in r.content

    asyncio.run(main())


class _RecordingProvider(FakeProvider):
    last_system: str | None = None

    async def stream(self, system, messages, tools):
        type(self).last_system = system
        async for ev in super().stream(system, messages, tools):
            yield ev


def test_skill_index_injected_into_system_prompt(tmp_path, monkeypatch):
    """Runner composes <available skills> into the system prompt (unit, no HTTP)."""
    prov = SkillToolProvider([_make_skill_dir(tmp_path / "skills")])
    registry = ToolRegistry([prov])
    db = Database(TEST_DB_URL)
    runs = RunManager(
        policy=Policy(),
        audit=None,
        max_concurrent=1,
        timeout_seconds=30,
        registry=registry,
    )
    session = SessionRow(
        id="s1",  # conftest 播种的 fixture 会话（messages 外键要求 sessions 有行）
        user_id=1, title="t", model="fake/demo",
        cwd=str(tmp_path), created_at="2026-09-27T00:00:00+00:00",
    )
    provider = _RecordingProvider(responses=[[TextBlock(text="hi")]])
    # run_turn resolves the provider from the model string; swap in the recorder
    monkeypatch.setattr(
        "pi.server.runner.resolve_chain",
        lambda model, on_fallback=None, **kw: provider,
    )

    async def main():
        await db.init()
        events = []
        async for ev in runs.run_turn(
            session=session,
            username="u1",
            user_id=1,
            prompt="hello",
            model="fake/demo",
            message_repo=MessageRepo(db),
        ):
            events.append(ev)
        assert _RecordingProvider.last_system is not None
        assert "<available skills>" in _RecordingProvider.last_system
        assert "code_review" in _RecordingProvider.last_system
        # no memory configured -> no memory block
        assert "<relevant memories>" not in _RecordingProvider.last_system
        await db.dispose()

    asyncio.run(main())
