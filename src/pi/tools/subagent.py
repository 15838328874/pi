"""Recursive sub-agent delegation.

Implements the Codex / ZCode-style "reuse yourself + depth cap" pattern: a
sub-agent is just another ``AgentLoop`` running the same tool set, with the
spawn tool's depth incremented by one per level. ``max_depth`` is the recursion
base case — at that depth the tool refuses to delegate any further.

Design notes (the things that matter, more than the code):

- **Recursion over a new executor**: no separate "sub-agent engine" — children
  are ``AgentLoop`` instances, so tools, policy, audit, tracer, and metering are
  inherited for free.
- **Context isolation**: a child receives only ``task`` (+ optional ``context``),
  never the parent's full history, so token cost stays bounded per level.
- **Error isolation**: a child that fails (provider error, tool crash, depth
  limit) becomes an ``ERROR`` result inline; it never aborts its siblings.
- **Workspace**: shared by default (a coding sub-agent must edit the project);
  ``isolated: true`` gives a fresh sub-directory to avoid concurrent-write
  conflicts between siblings.
- **Metering**: every child's ``Usage`` is summed and returned on the
  ``ToolResult`` so the parent loop can add it to the user's quota.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from pi.agent.events import ErrorEvent, TextDeltaEvent, TurnEndEvent
from pi.models import Usage
from pi.tools.base import Tool, ToolContext, ToolResult, truncate

SUBAGENT_SYSTEM_PROMPT = (
    "You are a sub-agent completing one subtask. Work autonomously: make "
    "reasonable assumptions instead of asking questions, use tools to inspect "
    "and modify files as needed, then return a concise summary of what you did "
    "and the outcome."
)

MAX_TASKS = 16
MAX_SUBAGENT_TURNS = 10


@dataclass
class _SubResult:
    task: str
    content: str
    is_error: bool
    usage: Usage


class SpawnSubagentsTool(Tool):
    """Delegate independent subtasks to sub-agents and aggregate their results.

    Each subtask runs as a separate ``AgentLoop`` (recursively reusing the same
    tools). Independent subtasks run concurrently; a failed subtask becomes an
    ``ERROR`` result and never aborts its siblings.
    """

    name = "spawn_subagents"
    capabilities = frozenset({"agent.delegate"})
    description = (
        "Delegate one or more INDEPENDENT subtasks to sub-agents and collect "
        "their results. Use this to parallelize work; give each subtask a clear, "
        "self-contained instruction and keep subtasks from touching the same "
        "files. `tasks` is a list of {task, context?, isolated?}."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string"},
                        "context": {"type": "string"},
                        "isolated": {
                            "type": "boolean",
                            "description": (
                                "run in a fresh sub-directory instead of the shared "
                                "workspace (use when siblings would touch the same files)"
                            ),
                        },
                    },
                    "required": ["task"],
                },
            }
        },
        "required": ["tasks"],
    }

    def __init__(self, depth: int = 0, max_depth: int = 3):
        super().__init__()
        self.depth = depth
        self.max_depth = max_depth

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # Recursion base case: never delegate past max_depth.
        if self.depth >= self.max_depth:
            return ToolResult(
                content=(
                    f"Error: sub-agent depth limit reached (depth={self.depth}, "
                    f"max={self.max_depth}); cannot delegate further."
                ),
                is_error=True,
            )

        raw = args.get("tasks")
        if not isinstance(raw, list) or not raw:
            return ToolResult(
                content="Error: `tasks` must be a non-empty list", is_error=True
            )
        if len(raw) > MAX_TASKS:
            return ToolResult(
                content=f"Error: at most {MAX_TASKS} subtasks per call", is_error=True
            )

        # Task distribution + parallelism: independent subtasks run concurrently.
        results = await asyncio.gather(
            *(self._run_one(index, spec, ctx) for index, spec in enumerate(raw))
        )
        return self._aggregate(results, ctx)

    async def _run_one(self, index: int, spec: Any, ctx: ToolContext) -> _SubResult:
        # Lazy imports keep tools/ importable without pulling in the agent loop
        # (avoiding a tools -> agent.loop -> tools.base import cycle).
        from pi.agent.loop import AgentLoop
        from pi.tools import all_tools

        if isinstance(spec, dict):
            task = str(spec.get("task", ""))
            context = str(spec.get("context", ""))
            isolated = bool(spec.get("isolated", False))
        else:
            task, context, isolated = str(spec), "", False

        # Workspace: shared by default (a coding sub-agent must edit the project);
        # `isolated` gives a fresh dir so siblings cannot overwrite each other.
        subdir = ctx.cwd
        if isolated:
            subdir = ctx.cwd / f".subagent_{uuid.uuid4().hex[:8]}"
            subdir.mkdir(parents=True, exist_ok=True)

        # Reuse the parent's provider so the child uses the same model / fallback
        # chain. Fall back to the default model if the context carries none.
        provider = getattr(ctx, "provider", None)
        if provider is None:
            from pi.llm.registry import DEFAULT_MODEL, resolve_chain

            provider = resolve_chain(DEFAULT_MODEL)

        child = AgentLoop(
            provider=provider,
            tools=all_tools(
                subagent_depth=self.depth + 1, max_subagent_depth=self.max_depth
            ),
            system_prompt=SUBAGENT_SYSTEM_PROMPT,
            messages=[],
            cwd=subdir,
            max_turns=MAX_SUBAGENT_TURNS,
            policy=getattr(ctx, "policy", None),
            audit=getattr(ctx, "audit", None),
            session_id=getattr(ctx, "session_id", ""),
            user_id=getattr(ctx, "user_id", ""),
            tracer=getattr(ctx, "tracer", None),
        )
        if ctx.runner is not None:
            child.ctx.runner = ctx.runner

        # Communication: task (+ optional context) is the child's whole input.
        prompt = task if not context else f"{task}\n\n<context>\n{context}\n</context>"

        text_parts: list[str] = []
        error: str | None = None
        usage = Usage()
        async for ev in child.run(prompt):
            if isinstance(ev, TextDeltaEvent):
                text_parts.append(ev.text)
            elif isinstance(ev, ErrorEvent):
                error = error or ev.message
            elif isinstance(ev, TurnEndEvent):
                usage = ev.usage

        if error is not None:
            return _SubResult(task=task, content=error, is_error=True, usage=usage)
        content = "".join(text_parts).strip() or "(no output)"
        return _SubResult(task=task, content=content, is_error=False, usage=usage)

    def _aggregate(self, results: list[_SubResult], ctx: ToolContext) -> ToolResult:
        total = Usage()
        lines: list[str] = []
        for i, r in enumerate(results, start=1):
            total = total.add(r.usage)
            lines.append(f"[{i}] task: {r.task}")
            lines.append(f"    status: {'ERROR' if r.is_error else 'OK'}")
            lines.append(f"    result: {r.content}")
        return ToolResult(
            content=truncate("\n".join(lines), ctx.max_output),
            is_error=False,  # the batch call itself succeeded; per-task errors are inline
            usage=total,
        )
