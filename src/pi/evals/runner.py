"""Run an eval task in-process and capture its trajectory.

The eval runner runs the agent exactly as a real turn would, then hands the
captured trajectory (P1) to a scorer. It is a CONSUMER of the runtime, not part
of it — the loop knows nothing about eval.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from pi.agent.loop import AgentLoop
from pi.evals.schema import RunResult, Task
from pi.llm.registry import DEFAULT_MODEL, resolve_chain
from pi.prompt import SYSTEM_PROMPT
from pi.tools import all_tools


async def run_task(
    task: Task,
    *,
    provider=None,
    model: str | None = None,
    workspace: Path | None = None,
    runner=None,
) -> RunResult:
    """Run one eval task and capture its trajectory.

    ``runner`` (optional, additive): a CommandRunner (pi.tools.sandbox). When
    given, bash tool calls AND the task's env.setup commands execute inside it
    (the rollout pipeline passes the Docker sandbox); None = host execution,
    the original dev-tool behavior. The scorer is wired separately via
    ``score(task, result, runner=...)``.
    """
    ws = workspace or Path(tempfile.mkdtemp(prefix="pi-eval-"))
    ws.mkdir(parents=True, exist_ok=True)
    ws_resolved = ws.resolve()

    # Seed the workspace with the task's initial files.
    for relpath, content in task.env.files.items():
        target = (ws / relpath).resolve()
        if not target.is_relative_to(ws_resolved):
            raise ValueError(
                f"task {task.id}: env.files path escapes workspace: {relpath!r}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    # Optional environment setup (shell commands).
    for cmd in task.env.setup:
        if runner is not None:
            await runner.run(cmd, str(ws), timeout=300)  # sandboxed setup
        else:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=str(ws),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()

    if provider is None:
        provider = resolve_chain(model or task.model or DEFAULT_MODEL)

    agent = AgentLoop(
        provider=provider,
        tools=all_tools(),
        system_prompt=SYSTEM_PROMPT,
        messages=[],
        cwd=ws,
    )
    if runner is not None:
        agent.ctx.runner = runner

    t0 = time.perf_counter()
    error: str | None = None
    try:
        async with asyncio.timeout(task.timeout):
            async for _ in agent.run(task.prompt):
                pass
    except TimeoutError:
        error = "timeout"
    except Exception as exc:  # noqa: BLE001 - a failed run is still a result
        error = f"{type(exc).__name__}: {exc}"

    traj = agent.trajectory.to_dict() if agent.trajectory is not None else {}
    finished = _last_event(traj, "RunFinished") or {}
    return RunResult(
        task_id=task.id,
        trajectory=traj,
        usage_input=finished.get("input_tokens", 0),
        usage_output=finished.get("output_tokens", 0),
        turns=finished.get("turns", 0),
        latency_ms=int((time.perf_counter() - t0) * 1000),
        workspace=str(ws),
        error=error,
    )


def _last_event(traj: dict, type_name: str) -> dict | None:
    for e in reversed(traj.get("events", [])):
        if e.get("type") == type_name:
            return e
    return None
