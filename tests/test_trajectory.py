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


def test_cost_budget_aborts_the_run():
    """max_cost_usd is a per-run estimated cost ceiling; exceeding it stops the
    run with an explicit error instead of silently burning budget."""
    # qwen3.8-max prices (1.6/6.4 per 1M) => one turn = 8e-6 USD; cap below that.
    provider = FakeProvider(
        model="qwen3.8-max", responses=[[TextBlock(text="x")] for _ in range(5)]
    )
    agent = AgentLoop(provider=provider, tools=all_tools(), max_cost_usd=0.000001)
    errors: list[str] = []

    async def main() -> None:
        async for ev in agent.run("hi"):
            if type(ev).__name__ == "ErrorEvent":
                errors.append(ev.message)

    asyncio.run(main())
    assert any("cost budget exceeded" in e for e in errors), errors
    # stopped after the first LLM round, before burning into a second turn
    assert agent.trajectory is not None
    finished = agent.trajectory.to_dict()["events"][-1]
    assert finished["turns"] == 1


def test_no_cost_budget_runs_normally():
    """max_cost_usd=0 (default) must not change behaviour."""
    provider = FakeProvider(responses=[[TextBlock(text="hi")]])
    agent = AgentLoop(provider=provider, tools=all_tools(), max_cost_usd=0.0)
    _run(agent, "hi")
    assert agent.trajectory is not None
    finished = agent.trajectory.to_dict()["events"][-1]
    assert finished["type"] == "RunFinished"


class TestDenialCircuitBreaker:
    """A model stuck retrying policy-denied calls aborts loudly instead of
    burning turns into max_turns/the run timeout (load-test observation:
    25 consecutive sandbox denials -> several 600s timeouts)."""

    def test_five_consecutive_denials_abort_the_run(self, tmp_path):
        import asyncio
        import json

        from pi.agent.loop import MAX_CONSECUTIVE_DENIALS, AgentLoop
        from pi.llm.fake import FakeProvider
        from pi.models import ToolCallBlock
        from pi.security.policy import Policy
        from pi.tools.bash import BashTool

        # script 2x the breaker: the breaker must fire before the script runs out
        responses = [
            [ToolCallBlock(id=f"c{i}", name="bash", arguments="{}")]
            for i in range(MAX_CONSECUTIVE_DENIALS * 2)
        ]
        agent = AgentLoop(
            provider=FakeProvider(responses=responses),
            # the tool must EXIST for the call to reach the policy gate;
            # an unknown tool errors before policy and never counts as denied
            tools=[BashTool()],
            policy=Policy(deny_tools={"bash"}),
            max_turns=40,
        )
        errors = []

        async def main():
            async for ev in agent.run("do the thing"):
                if type(ev).__name__ == "ErrorEvent":
                    errors.append(ev.message)
            return agent

        agent = asyncio.run(main())
        assert any("consecutive tool calls were denied" in e for e in errors), errors
        tool_calls = [
            e for e in agent.trajectory.to_dict()["events"] if e["type"] == "ToolCall"
        ]
        assert len(tool_calls) == MAX_CONSECUTIVE_DENIALS  # stopped at the threshold
        assert all(e["denied"] for e in tool_calls)


class TestProviderProxyHygiene:
    """Model traffic must not be hijacked by ambient proxy env vars (observed
    live: dead SOCKS proxy -> socksio ImportError -> every LLM call failed)."""

    def test_openai_provider_trust_env_false(self):
        from pi.llm.openai_provider import OpenAIProvider

        p = OpenAIProvider(model="x", api_key="k", base_url="http://127.0.0.1:1/v1")
        assert p.client._client.trust_env is False

    def test_anthropic_provider_trust_env_false(self):
        from pi.llm.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(model="x", api_key="k")
        assert p.client._client.trust_env is False
