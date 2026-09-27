"""Tests for the canonical run trajectory (P1 of the eval stack)."""

from __future__ import annotations

import asyncio
import json

from pi.agent.loop import AgentLoop
from pi.llm.fake import FakeProvider
from pi.models import TextBlock, ToolCallBlock
from pi.security.policy import Policy
from pi.tools import all_tools


def _run(agent: AgentLoop, prompt: str) -> None:
    async def main():
        async for _ in agent.run(prompt):
            pass

    asyncio.run(main())


def test_trajectory_records_full_run(tmp_path):
    provider = FakeProvider(
        model="demo",
        responses=[
            [
                ToolCallBlock(
                    id="t1",
                    name="write",
                    arguments=json.dumps({"path": "a.txt", "content": "hi"}),
                )
            ],
            [TextBlock(text="done")],
        ],
    )
    agent = AgentLoop(
        provider=provider, tools=all_tools(), system_prompt="sys", messages=[], cwd=tmp_path
    )
    _run(agent, "write a file")

    assert agent.trajectory is not None
    d = agent.trajectory.to_dict()
    assert d["run_id"]
    types = [e["type"] for e in d["events"]]
    assert types[0] == "RunStarted"
    assert types[-1] == "RunFinished"
    assert types.count("LlmCall") == 2  # turn 1 (tool call) + turn 2 (text)
    assert types.count("ToolCall") == 1

    started = d["events"][0]
    assert started["prompt"] == "write a file"
    assert "write" in started["tools"]
    assert started["model"] == "demo"

    tc = next(e for e in d["events"] if e["type"] == "ToolCall")
    assert tc["name"] == "write"
    assert tc["arguments"] == {"path": "a.txt", "content": "hi"}
    assert tc["is_error"] is False
    assert tc["denied"] is False
    assert tc["latency_ms"] >= 0

    finished = d["events"][-1]
    assert finished["turns"] == 2
    assert finished["output_tokens"] == 2  # two StreamEnds, each 1 output token

    # the whole thing is JSON-serializable (that's what eval will consume)
    json.dumps(d)


def test_trajectory_records_denied_tool(tmp_path):
    policy = Policy.from_dict({"deny_tools": ["write"]})
    provider = FakeProvider(
        model="demo",
        responses=[
            [
                ToolCallBlock(
                    id="t1",
                    name="write",
                    arguments=json.dumps({"path": "x", "content": "y"}),
                )
            ],
            [TextBlock(text="ok")],
        ],
    )
    agent = AgentLoop(provider=provider, tools=all_tools(), policy=policy, cwd=tmp_path)
    _run(agent, "write x")

    tc = next(e for e in agent.trajectory.to_dict()["events"] if e["type"] == "ToolCall")
    assert tc["denied"] is True
    assert tc["is_error"] is True
    assert "denied by security policy" in tc["result"]


def test_trajectory_records_error():
    class Boom(FakeProvider):
        async def stream(self, system, messages, tools):
            raise RuntimeError("boom")
            yield None  # pragma: no cover - keep it an async generator

    agent = AgentLoop(provider=Boom(model="demo"), tools=all_tools(), messages=[])
    _run(agent, "hi")

    d = agent.trajectory.to_dict()
    types = [e["type"] for e in d["events"]]
    assert "RunError" in types
    err = next(e for e in d["events"] if e["type"] == "RunError")
    assert "RuntimeError" in err["message"]
    # even after an error the run is marked finished
    assert types[-1] == "RunFinished"
