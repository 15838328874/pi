"""Batch rollout: the RL data flywheel's collection stage.

For every task, run n_samples independent rollouts (GRPO needs a group per
prompt for within-group advantage), capture the P1 trajectory, score it with
the verifiable/ judge scorers, and turn the verdict into a reward.

Each sample gets a FRESH workspace (tempdir, removed afterwards) so group
members are independent; when ``sandbox`` is set the agent runs inside the
Docker sandbox and scorer shells execute there too - rollout data is collected
in the production execution environment, not on the developer host.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pi.evals.reward import extract_reward
from pi.evals.runner import run_task
from pi.evals.schema import Task
from pi.evals.scorers import score
from pi.tools.sandbox import get_runner


@dataclass
class RolloutSample:
    task_id: str
    prompt: str
    trajectory: dict
    messages: list[dict]  # OpenAI chat format, no system message
    reward: float
    reward_meta: dict
    usage_input: int
    usage_output: int
    latency_ms: int
    error: str | None = None
    extra: dict = field(default_factory=dict)


async def rollout(
    task_set: list[Task],
    *,
    model: str,
    n_samples: int = 8,
    concurrency: int = 16,
    sandbox: str = "",
    judge_model: str | None = None,
    partial_credit: bool = True,
    provider=None,
) -> list[RolloutSample]:
    """Run n_samples rollouts per task; concurrency caps parallel samples.

    sandbox: "" = host (dev), "docker" = container (production-like). The
    runner is the process-level singleton (get_runner), shared across samples
    like the server's warm pool. ``provider`` override is for tests/scripts:
    pass a FACTORY (callable returning a fresh provider per sample) so
    concurrent rollouts never share stateful clients; by default it resolves
    from ``model``.
    """
    runner = get_runner(sandbox) if sandbox else None
    sem = asyncio.Semaphore(concurrency)

    async def one(task: Task, i: int) -> RolloutSample:
        async with sem:
            sample_provider = provider() if callable(provider) else provider
            workspace = Path(tempfile.mkdtemp(prefix="pi-rollout-"))
            try:
                result = await run_task(
                    task,
                    provider=sample_provider,
                    model=model,
                    workspace=workspace,
                    runner=runner,
                )
                verdict = await score(
                    task, result, judge_model=judge_model, runner=runner
                )
                reward, meta = extract_reward(verdict, partial_credit=partial_credit)
                return RolloutSample(
                    task_id=task.id,
                    prompt=task.prompt,
                    trajectory=result.trajectory,
                    messages=_trajectory_messages(result.trajectory),
                    reward=reward,
                    reward_meta=meta,
                    usage_input=result.usage_input,
                    usage_output=result.usage_output,
                    latency_ms=result.latency_ms,
                    error=result.error,
                )
            finally:
                shutil.rmtree(workspace, ignore_errors=True)

    samples = await asyncio.gather(
        *[one(task, i) for task in task_set for i in range(n_samples)]
    )
    return list(samples)


def _trajectory_messages(traj: dict) -> list[dict]:
    """Rebuild the conversation in OpenAI chat format from the canonical log.

    RunStarted.prompt -> user message; each LlmCall -> one assistant message
    (text + tool_calls from that turn); each ToolCall -> a tool message. This
    mirrors the block model the loop appended, since the trajectory (not the
    raw Message list) is what RunResult carries.
    """
    messages: list[dict] = []
    events = traj.get("events", [])
    for e in events:
        if e.get("type") == "RunStarted":
            messages.append({"role": "user", "content": e.get("prompt", "")})
        elif e.get("type") == "LlmCall":
            msg: dict = {"role": "assistant", "content": e.get("text", "")}
            calls = e.get("tool_calls") or []
            if calls:
                msg["tool_calls"] = [
                    {
                        "id": c.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": c.get("name", ""),
                            "arguments": c.get("arguments", "{}"),
                        },
                    }
                    for c in calls
                ]
            messages.append(msg)
        elif e.get("type") == "ToolCall":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": e.get("call_id", ""),
                    "content": e.get("result", ""),
                }
            )
    return messages
