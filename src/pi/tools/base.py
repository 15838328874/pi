"""Tool interface shared by all built-in tools (pi-coding-agent/tools analogue)."""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import difflib


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    payload: Any = None  # structured side-product; the loop turns it into an event
    usage: Any = None  # optional Usage for tools that run their own LLM loops (sub-agents)  # noqa: E501


class WorkspaceFS(Protocol):
    """File-system abstraction the file tools operate on.

    Default is the host workspace (LocalFS). A sandboxed runner may replace it
    with a remote FS (e.g. CubeSandbox files API) so read/write/edit/... act on
    the sandbox's own filesystem - the model's path perception then matches the
    files bash actually runs against, with no per-call workspace syncing.
    """

    async def read_bytes(self, path: Path) -> bytes | None:
        """Whole file bytes, or None when missing / unreadable."""
        ...

    async def write_bytes(self, path: Path, data: bytes) -> None:
        """Create parents, overwrite whole file."""
        ...

    async def exists(self, path: Path) -> bool:
        ...

    async def is_dir(self, path: Path) -> bool:
        ...

    async def list_dir(self, path: Path) -> list[tuple[str, bool, int]]:
        """[(name, is_dir, size_bytes)] - one level, sorted by name."""
        ...

    async def walk(self, path: Path) -> list[Path]:
        """Recursive paths (files + dirs), SKIP_DIRS excluded, capped for safety."""
        ...

    async def file_size(self, path: Path) -> int | None:
        """Size in bytes, or None when missing."""
        ...


class LocalFS:
    """Host filesystem - the historical behaviour of the file tools."""

    async def read_bytes(self, path: Path) -> bytes | None:
        try:
            return await asyncio.to_thread(path.read_bytes)
        except OSError:
            return None

    async def write_bytes(self, path: Path, data: bytes) -> None:
        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await asyncio.to_thread(_write)

    async def exists(self, path: Path) -> bool:
        return await asyncio.to_thread(path.exists)

    async def is_dir(self, path: Path) -> bool:
        return await asyncio.to_thread(path.is_dir)

    async def list_dir(self, path: Path) -> list[tuple[str, bool, int]]:
        def _list() -> list[tuple[str, bool, int]]:
            out = []
            for e in sorted(path.iterdir(), key=lambda e: e.name.lower()):
                try:
                    out.append((e.name, e.is_dir(), e.stat().st_size if e.is_file() else 0))
                except OSError:
                    out.append((e.name, False, 0))
            return out

        return await asyncio.to_thread(_list)

    async def walk(self, path: Path) -> list[Path]:
        def _walk() -> list[Path]:
            out: list[Path] = []
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for name in dirnames:
                    out.append(Path(dirpath) / name)
                for name in filenames:
                    out.append(Path(dirpath) / name)
                if len(out) >= 20_000:
                    break
            return out

        return await asyncio.to_thread(_walk)

    async def file_size(self, path: Path) -> int | None:
        def _size() -> int | None:
            try:
                return path.stat().st_size
            except OSError:
                return None

        return await asyncio.to_thread(_size)


def default_fs() -> WorkspaceFS:
    return LocalFS()


def get_fs(ctx: ToolContext) -> WorkspaceFS:
    """The workspace filesystem for a tool context (host by default,
    sandbox filesystem when a sandboxed runner provides one)."""
    return ctx.fs if ctx.fs is not None else LocalFS()


@dataclass
class ToolContext:
    cwd: Path = field(default_factory=Path.cwd)
    max_output: int = 30_000
    runner: Any = None  # CommandRunner (pi.tools.sandbox); None -> local shell
    fs: WorkspaceFS | None = None  # file tools' filesystem; None -> LocalFS
    # 惰性沙箱钩子（会话级池）：None 表示回合以本地模式起步，第一次需要沙箱的
    # 工具（bash）调用前由 runner.py 注入的可等待回调现场创建 VM。纯聊天回合
    # 永不触发，零沙箱开销。
    ensure_runner: Any = None  # Callable[[], Awaitable[None]] | None
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
    files: Any = None  # FileRepo, injected by the server runner (list_files/fetch_file)
    store: Any = None  # ObjectStore, presigned URLs for the file pipeline


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
