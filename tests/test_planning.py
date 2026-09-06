"""submit_plan: the tool, and the loop's terminal-tool semantics.

The batch rules are the load-bearing part of this feature. OpenAI and Anthropic
both reject a request whose assistant message contains a tool_call with no
matching tool_result in the next user message - and they reject it on the *next*
request, not the one that produced it. So a loop that ends a batch early without
answering the calls it skipped writes history that looks fine and then 400s a turn
later. StrictFakeProvider exists to move that failure into this file.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pi.agent.events import (
    ErrorEvent,
    PlanEvent,
    TextDeltaEvent,
    ToolCallEndEvent,
    TurnEndEvent,
)
from pi.agent.loop import PREVIEW_LEN, AgentLoop
from pi.llm.fake import FakeProvider
from pi.models import Message, Plan, Role, TextBlock, ToolCallBlock, ToolResultBlock
from pi.security.audit import AuditLogger
from pi.tools import all_tools
from pi.tools.base import ToolContext
from pi.tools.plan import SubmitPlanTool
from conftest import StrictFakeProvider


def tc(id: str, name: str, args: dict | str) -> ToolCallBlock:
    return ToolCallBlock(
        id=id, name=name, arguments=args if isinstance(args, str) else json.dumps(args)
    )


PLAN_ARGS = {
    "title": "重构 sessions 表",
    "steps": ["加可空 plan 列", "写迁移 0003", "补 DDL 可移植性测试"],
}


def _results(msg: Message) -> list[ToolResultBlock]:
    return [b for b in msg.blocks if isinstance(b, ToolResultBlock)]


def _text(events: list) -> str:
    return "".join(e.text for e in events if isinstance(e, TextDeltaEvent))


def _turns(events: list) -> int:
    return [e for e in events if isinstance(e, TurnEndEvent)][0].turns


def _plans(events: list) -> list[PlanEvent]:
    return [e for e in events if isinstance(e, PlanEvent)]


class TestSubmitPlanTool:
    def _execute(self, args: dict, tmp_path: Path):
        async def main():
            return await SubmitPlanTool().execute(args, ToolContext(cwd=tmp_path))

        return asyncio.run(main())

    def test_records_the_plan_and_returns_it_as_payload(self, tmp_path: Path):
        out = self._execute(PLAN_ARGS, tmp_path)
        assert out.is_error is False
        assert isinstance(out.payload, Plan)
        assert out.payload.title == PLAN_ARGS["title"]
        assert out.payload.steps == PLAN_ARGS["steps"]
        assert "3 steps" in out.content

    def test_it_touches_nothing_on_disk(self, tmp_path: Path):
        """The tool is pure: it validates and returns. No file, no context state."""
        self._execute(PLAN_ARGS, tmp_path)
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param({**PLAN_ARGS, "steps": ["s"] * 21}, id="too-many-steps"),
            pytest.param({**PLAN_ARGS, "steps": ["x" * 301]}, id="step-too-long"),
            pytest.param({**PLAN_ARGS, "title": ""}, id="empty-title"),
            pytest.param({"title": "只有标题"}, id="missing-steps"),
            pytest.param({**PLAN_ARGS, "steps": []}, id="no-steps"),
        ],
    )
    def test_rejects_oversized_and_malformed_plans(self, args: dict, tmp_path: Path):
        out = self._execute(args, tmp_path)
        assert out.is_error is True
        assert out.content.startswith("Error: invalid plan")
        assert out.payload is None

    def test_validation_error_content_is_truncated(self, tmp_path: Path):
        """A 500-step submission dumps several KB of pydantic detail, and that text
        goes straight back into the model's history."""
        out = self._execute({**PLAN_ARGS, "steps": [""] * 500}, tmp_path)
        assert out.is_error is True
        assert len(out.content) <= 600

    def test_success_content_fits_the_sse_preview(self, tmp_path: Path):
        """event_to_sse inherits the loop's PREVIEW_LEN cut, so a longer
        confirmation would reach the browser as a half sentence."""
        out = self._execute({**PLAN_ARGS, "steps": ["s"] * 20}, tmp_path)
        assert len(out.content) <= PREVIEW_LEN

    def test_it_is_registered_and_marked_terminal(self):
        tools = {t.name: t for t in all_tools()}
        assert "submit_plan" in tools
        assert tools["submit_plan"].terminal is True
        assert all(not t.terminal for n, t in tools.items() if n != "submit_plan")


class TestSubmitPlanLoop:
    def _run(self, provider: FakeProvider, tmp_path: Path, audit: AuditLogger | None = None):
        async def main():
            agent = AgentLoop(
                provider=provider,
                tools=all_tools(),
                system_prompt="test",
                messages=[],
                cwd=tmp_path,
                audit=audit,
                session_id="s1",
                user_id="alice",
            )
            events = [ev async for ev in agent.run("重构 sessions 表")]
            return agent, events

        return asyncio.run(main())

    def test_a_successful_submit_plan_ends_the_run(self, tmp_path: Path):
        provider = FakeProvider(
            responses=[
                [TextBlock(text="先规划。"), tc("c1", "submit_plan", PLAN_ARGS)],
                [TextBlock(text="这一段不应该被流出")],
            ]
        )
        agent, events = self._run(provider, tmp_path)

        assert not [e for e in events if isinstance(e, ErrorEvent)]
        plans = _plans(events)
        assert len(plans) == 1
        assert plans[0].plan.title == PLAN_ARGS["title"]

        kinds = [type(e).__name__ for e in events]
        last_end = max(i for i, k in enumerate(kinds) if k == "ToolCallEndEvent")
        assert last_end < kinds.index("PlanEvent") < kinds.index("TurnEndEvent")

        assert _turns(events) == 1
        assert "先规划" in _text(events)
        assert "不应该" not in _text(events)
        assert provider.responses, "the second scripted batch must not have been consumed"

        assert [m.role for m in agent.messages] == [Role.user, Role.assistant, Role.user]
        results = _results(agent.messages[2])
        assert len(results) == 1
        assert results[0].is_error is False
        assert "Plan recorded" in results[0].content

    def test_later_calls_in_the_batch_are_skipped_without_executing(self, tmp_path: Path):
        provider = FakeProvider(
            responses=[
                [
                    tc("c1", "submit_plan", PLAN_ARGS),
                    tc("c2", "write", {"path": "x.txt", "content": "leak"}),
                    tc("c3", "bash", {"command": "echo ran"}),
                ],
                [TextBlock(text="never")],
            ]
        )
        agent, events = self._run(provider, tmp_path)

        assert not (tmp_path / "x.txt").exists(), "a skipped write must not touch the disk"
        assert len(_plans(events)) == 1
        assert _turns(events) == 1

        ends = [e for e in events if isinstance(e, ToolCallEndEvent)]
        assert [(e.id, e.ok) for e in ends] == [("c1", True), ("c2", False), ("c3", False)]
        assert all("did not execute" in e.result for e in ends[1:])

        results = _results(agent.messages[2])
        assert [r.tool_use_id for r in results] == ["c1", "c2", "c3"]
        assert results[0].is_error is False
        for r in results[1:]:
            assert r.is_error is True
            assert "did not execute" in r.content
            assert "no side effects" in r.content

    def test_skipped_calls_are_audited(self, tmp_path: Path):
        """A skip is the loop refusing to run something the model asked for, which is
        exactly what an operator tailing the audit log needs to see - including the
        arguments that were dropped."""
        audit = AuditLogger(tmp_path / "audit.jsonl")
        provider = FakeProvider(
            responses=[
                [
                    tc("c1", "submit_plan", PLAN_ARGS),
                    tc("c2", "write", {"path": "x.txt", "content": "leak"}),
                    tc("c3", "bash", {"command": "echo ran"}),
                ],
            ]
        )
        self._run(provider, tmp_path, audit=audit)

        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = (tmp_path / f"audit-{day}.jsonl").read_text(encoding="utf-8").strip().splitlines()
        records = [json.loads(line) for line in lines]
        assert [r["tool"] for r in records] == ["submit_plan", "write", "bash"]

        assert records[0]["allowed"] is True
        assert records[0]["ok"] is True

        for r in records[1:]:
            assert r["allowed"] is False
            assert "skipped" in r["reason"]
            assert r["ok"] is None
        assert records[1]["args"]["path"] == "x.txt"
        assert records[2]["args"]["command"] == "echo ran"

    def test_an_invalid_plan_does_not_end_the_turn(self, tmp_path: Path):
        """Ending on a failed submit_plan would leave the user with neither a plan
        nor an answer. The error goes back and the model gets to fix it."""
        provider = FakeProvider(
            responses=[
                [tc("c1", "submit_plan", {"title": "t", "steps": []})],
                [TextBlock(text="改好了，这是答案")],
            ]
        )
        agent, events = self._run(provider, tmp_path)

        assert not _plans(events)
        assert _results(agent.messages[2])[0].is_error is True
        assert "invalid plan" in _results(agent.messages[2])[0].content
        assert "改好了" in _text(events)
        assert _turns(events) == 2

    def test_the_first_of_two_submit_plans_in_one_batch_wins(self, tmp_path: Path):
        provider = FakeProvider(
            responses=[
                [
                    tc("c1", "submit_plan", {**PLAN_ARGS, "title": "第一个"}),
                    tc("c2", "submit_plan", {**PLAN_ARGS, "title": "第二个"}),
                ],
            ]
        )
        agent, events = self._run(provider, tmp_path)

        plans = _plans(events)
        assert len(plans) == 1
        assert plans[0].plan.title == "第一个"
        results = _results(agent.messages[2])
        assert results[0].is_error is False
        assert results[1].is_error is True
        assert "did not execute" in results[1].content
        assert _turns(events) == 1

    def test_history_stays_paired_for_the_next_turn(self, tmp_path: Path):
        """The reason skip results are synthesized at all.

        The failure is invisible in the run that causes it: the batch ends, the
        turn ends, everything looks fine. It surfaces one turn later, when the
        history is loaded and sent to a provider that checks it. This test does the
        same thing the server does - serialize the messages, read them back, run
        again - against a provider that enforces the pairing rule.
        """

        async def main():
            provider = StrictFakeProvider(
                responses=[
                    [
                        tc("c1", "submit_plan", PLAN_ARGS),
                        tc("c2", "bash", {"command": "echo ran"}),
                    ],
                    [TextBlock(text="继续执行计划")],
                ]
            )
            first = AgentLoop(
                provider=provider, tools=all_tools(), system_prompt="test",
                messages=[], cwd=tmp_path,
            )
            ev1 = [ev async for ev in first.run("重构 sessions 表")]
            history = [Message.model_validate_json(m.model_dump_json()) for m in first.messages]
            second = AgentLoop(
                provider=provider, tools=all_tools(), system_prompt="test",
                messages=history, cwd=tmp_path,
            )
            ev2 = [ev async for ev in second.run("继续")]
            return ev1, ev2

        ev1, ev2 = asyncio.run(main())

        for ev in ev1 + ev2:
            assert not isinstance(ev, ErrorEvent), ev.message
        assert len(_plans(ev1)) == 1
        assert "继续执行计划" in _text(ev2)
