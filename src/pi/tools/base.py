"""Tool interface shared by all built-in tools (pi-coding-agent/tools analogue)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    usage: Any = None  # optional Usage for tools that run their own LLM loops (sub-agents)


@dataclass
class ToolContext:
    cwd: Path = field(default_factory=Path.cwd)
    max_output: int = 30_000
    runner: Any = None  # CommandRunner (pi.tools.sandbox); None -> local shell
    # Sub-agent spawning deps, populated by AgentLoop so a tool can delegate to a
    # child loop with the same model / policy / audit / tracer context.
    provider: Any = None
    policy: Any = None
    audit: Any = None
    tracer: Any = None
    session_id: str = ""
    user_id: str = ""
    user_db_id: int | None = None  # integer user id (for DB-scoped stores like memory)
    memory: Any = None  # MemoryRepo, injected by the server runner


class Tool(ABC):
    name: str = "abstract"
    description: str = ""
    input_schema: dict[str, Any] = {}

    @abstractmethod
    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"


def resolve_path(ctx: ToolContext, raw: str) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = ctx.cwd / p
    return p.resolve()


SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".pi-py",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".idea",
    ".vscode",
}
