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
    payload: Any = None  # structured side-product; the loop turns it into an event


@dataclass
class ToolContext:
    cwd: Path = field(default_factory=Path.cwd)
    max_output: int = 30_000
    runner: Any = None  # CommandRunner (pi.tools.sandbox); None -> local shell


class Tool(ABC):
    name: str = "abstract"
    description: str = ""
    input_schema: dict[str, Any] = {}
    # A terminal tool ends the turn as soon as it succeeds: the loop stops
    # asking the provider for more work and synthesizes an error result for
    # every remaining call in the batch (see AgentLoop._run_inner). The flag
    # lives here rather than as a tool name in the loop because the loop is
    # deliberately generic - it hardcodes no tool names today, and a second
    # terminal tool should not require editing its heart.
    terminal: bool = False

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
