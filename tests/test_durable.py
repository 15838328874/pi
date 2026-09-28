"""Tests for durable execution (P2): checkpoint + resume."""

from __future__ import annotations

import asyncio
import json

from pi.agent.loop import AgentLoop, Checkpoint
from pi.llm.fake import FakeProvider
from pi.models import Role, TextBlock, ToolCallBlock
from pi.tools import all_tools
from pi.tools.base import Tool, ToolContext, ToolResult


def _drain(agent, *args, **kwargs):
    async def main():
        async for _ in agent.run(*args, **kwargs):
            pass

    asyncio.run(main())


def test_checkpoint_emitted_after_each_step(tmp_path):
    provider = FakeProvider(
        model="demo",
        responses=[
            [
                ToolCallBlock(
                    id="t1",
                    name="write",
                    arguments=json.dumps({"path": "a.txt", "content": "A"}),
                )
            ],
            [
                ToolCallBlock(
                    id="t2",
                    name="write",
                    arguments=json.dumps({"path": "b.txt", "content": "B"}),
                )
            ],
            [TextBlock(text="done")],
        ],
    )
    checkpoints: list[Checkpoint] = []
    agent = AgentLoop(
        provider=provider, tools=all_tools(), cwd=tmp_path, on_checkpoint=checkpoints.append
    )
    _drain(agent, "do two writes")

    # two tool steps -> two checkpoints (the final text turn has no tools)
    assert len(checkpoints) == 2
    assert checkpoints[0].step == 1
    assert checkpoints[1].step == 2
    assert checkpoints[0].turns == 1
    # each checkpoint carries the running usage and the full history so far
    assert checkpoints[0].input_tokens == 1
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "A"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "B"


def test_checkpoint_roundtrips_and_resumes(tmp_path):
    provider = FakeProvider(
        model="demo",
        responses=[
            [
                ToolCallBlock(
                    id="t1",
                    name="write",
                    arguments=json.dumps({"path": "a.txt", "content": "A"}),
                )
            ],
            [TextBlock(text="step one done")],
        ],
    )
    checkpoints: list[Checkpoint] = []
    agent = AgentLoop(
        provider=provider, tools=all_tools(), cwd=tmp_path, on_checkpoint=checkpoints.append
    )
    _drain(agent, "write a then stop")
    assert len(checkpoints) == 1

    # serialize -> deserialize (this is the durable boundary)
    cp = Checkpoint.from_dict(checkpoints[0].to_dict())
    assert cp.turns == 1
    msgs_before = len(cp.messages)

    # resume: a fresh agent continues from the checkpoint with a fresh provider
    provider2 = FakeProvider(model="demo", responses=[[TextBlock(text="resumed done")]])
    agent2 = AgentLoop(provider=provider2, tools=all_tools(), cwd=tmp_path)
    _drain(agent2, resume_from=cp)

    # no new user message was appended; history continued from the checkpoint
    assert agent2.messages[0].role == Role.user
    assert len(agent2.messages) == msgs_before + 1
    assert agent2.messages[-1].blocks[0].text == "resumed done"
    # token usage carried over (1 input from step 1 + 1 from the resumed turn)
    assert agent2.trajectory is not None
    finished = next(
        e for e in agent2.trajectory.to_dict()["events"] if e["type"] == "RunFinished"
    )
    assert finished["input_tokens"] == 2


class _CountingTool(Tool):
    """Side-effect probe: counts executions to prove idempotent replay."""

    name = "side_effect"
    description = "increments a counter"
    input_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }
    calls = 0

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        type(self).calls += 1
        return ToolResult(content=f"executed #{type(self).calls}")


def test_resume_replays_completed_tool(tmp_path):
    """A resume must not re-execute a tool already completed in the checkpoint.

    Simulates the crash->resume window: the model re-issues the same (name, args)
    tool call, and the loop must replay the recorded result rather than running
    the side effect again.
    """
    _CountingTool.calls = 0
    provider1 = FakeProvider(
        model="demo",
        responses=[
            [
                ToolCallBlock(
                    id="t1", name="side_effect", arguments=json.dumps({"value": "x"})
                )
            ]
        ],
    )
    checkpoints: list[Checkpoint] = []
    agent1 = AgentLoop(
        provider=provider1,
        tools=[_CountingTool()],
        cwd=tmp_path,
        on_checkpoint=checkpoints.append,
    )
    _drain(agent1, "do it once")
    assert _CountingTool.calls == 1
    assert len(checkpoints) == 1
    # the ledger records the completed tool keyed by (name, canonical args)
    key = "side_effect|" + json.dumps({"value": "x"}, sort_keys=True)
    assert key in checkpoints[0].completed_tools

    # resume with a provider that re-issues the SAME tool call
    cp = Checkpoint.from_dict(checkpoints[0].to_dict())
    provider2 = FakeProvider(
        model="demo",
        responses=[
            [
                ToolCallBlock(
                    id="t2", name="side_effect", arguments=json.dumps({"value": "x"})
                )
            ]
        ],
    )
    agent2 = AgentLoop(provider=provider2, tools=[_CountingTool()], cwd=tmp_path)
    _drain(agent2, resume_from=cp)

    # replayed, not re-executed
    assert _CountingTool.calls == 1


def test_same_tool_repeat_in_normal_run_is_not_deduped(tmp_path):
    """Idempotent replay is gated on resume: within one normal run, two
    identical tool calls must BOTH execute (no false dedup)."""
    _CountingTool.calls = 0
    provider = FakeProvider(
        model="demo",
        responses=[
            [
                ToolCallBlock(
                    id="t1", name="side_effect", arguments=json.dumps({"value": "x"})
                )
            ],
            [
                ToolCallBlock(
                    id="t2", name="side_effect", arguments=json.dumps({"value": "x"})
                )
            ],
        ],
    )
    agent = AgentLoop(provider=provider, tools=[_CountingTool()], cwd=tmp_path)
    _drain(agent, "do it twice")
    assert _CountingTool.calls == 2
