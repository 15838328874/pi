"""Tests for durable execution (P2): checkpoint + resume."""

from __future__ import annotations

import asyncio
import json

from pi.agent.loop import AgentLoop, Checkpoint
from pi.llm.fake import FakeProvider
from pi.models import Role, TextBlock, ToolCallBlock
from pi.tools import all_tools


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
