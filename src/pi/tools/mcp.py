"""MCP (Model Context Protocol) tools: external tool sources via the official SDK.

Transport: stdio (spawn a process) or HTTP (streamable HTTP / SSE). Config:
PI_MCP_SERVERS = JSON array, e.g.
  [{"name":"filesystem","command":["npx","-y","@modelcontextprotocol/server-filesystem","/ws"]}]
or
  [{"name":"remote","url":"https://host/mcp"}]

Security note: stdio servers are spawned as child processes of the app. They do
NOT inherit the app's environment - the MCP SDK spawns them with a safe
allow-list (HOME/LOGNAME/PATH/SHELL/TERM/USER via get_default_environment), so
PI_JWT_SECRET / PI_DATABASE_URL never reach them. They DO run unsandboxed (the
app's uid, filesystem and network), so PI_MCP_SERVERS is still admin-level
config. Their tools go through the same policy gate / audit / tracing as builtin
tools, and path/file/dir args are workspace-confined by the generic path sandbox
(best-effort, key-name based - see security.policy._extract_paths).

v1: no automatic reconnect - a dead server surfaces as per-call tool errors
until the server process restarts (TODO: health check + reconnect).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any

from pi.tools.base import Tool, ToolContext, ToolResult, truncate
from pi.tools.registry import ToolProvider

log = logging.getLogger("pi.tools.mcp")


class McpTool(Tool):
    def __init__(
        self,
        session: Any,
        lock: asyncio.Lock,
        name: str,
        description: str,
        input_schema: dict[str, Any],
    ) -> None:
        self.name = name
        self.description = description or ""
        self.input_schema = input_schema or {"type": "object", "properties": {}}
        self._session = session
        # One lock per session (shared by every tool on it): concurrent calls
        # from different user sessions must not interleave on one transport.
        self._lock = lock

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            async with self._lock:
                result = await self._session.call_tool(self.name, arguments=args or {})
        except Exception as exc:  # noqa: BLE001 - server death is a tool error, not a crash
            return ToolResult(
                content=f"Error: MCP tool {self.name} failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )
        return ToolResult(
            content=truncate(_format_result(result), ctx.max_output),
            is_error=bool(result.is_error),
        )


def _format_result(result: Any) -> str:
    """tools/call -> {content:[{type:text,text:...}], isError}; join the text."""
    parts: list[str] = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        elif hasattr(block, "model_dump_json"):
            parts.append(block.model_dump_json())
        else:
            parts.append(str(block))
    return "\n".join(parts)


class McpToolProvider(ToolProvider):
    """Aggregates every configured MCP server; one dead server never takes the
    others down (fail-soft, logged)."""

    name = "mcp"

    def __init__(self, servers: list[dict[str, Any]]):
        self.servers = servers
        self._stacks: list[AsyncExitStack] = []
        self._session_locks: dict[int, asyncio.Lock] = {}

    async def tools(self) -> list[Tool]:
        out: list[Tool] = []
        for cfg in self.servers:
            name = cfg.get("name", "?")
            try:
                session, stack = await _connect(cfg)
            except Exception as exc:  # noqa: BLE001 - fail-soft per server
                log.warning("MCP server %r failed to connect: %s", name, exc)
                continue
            self._stacks.append(stack)
            try:
                specs = await session.list_tools()
            except Exception as exc:  # noqa: BLE001
                log.warning("MCP server %r list_tools failed: %s", name, exc)
                continue
            lock = self._session_locks.setdefault(id(session), asyncio.Lock())
            for spec in specs.tools:
                out.append(
                    McpTool(
                        session,
                        lock,
                        spec.name,
                        # NB: snake_case - accessing the camelCase alias deadlocks
                        # in mcp 2.x (lazy validation through a missing portal).
                        spec.description or "",
                        spec.input_schema or {},
                    )
                )
            log.info("MCP server %r: %d tools", name, len(specs.tools))
        return out

    async def close(self) -> None:
        for stack in reversed(self._stacks):
            try:
                await stack.aclose()
            except Exception:  # noqa: BLE001 - teardown must not block shutdown
                log.debug("MCP connection close failed", exc_info=True)
        self._stacks.clear()


async def _connect(cfg: dict[str, Any]) -> tuple[Any, AsyncExitStack]:
    """Connect one server config: {"command": [...]} stdio, or {"url": ...} HTTP."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client

    stack = AsyncExitStack()
    if cfg.get("command"):
        params = StdioServerParameters(
            command=str(cfg["command"][0]),
            args=[str(a) for a in cfg["command"][1:]],
            env={str(k): str(v) for k, v in cfg.get("env", {}).items()} or None,
        )
        read, write = await stack.enter_async_context(stdio_client(params))
    elif cfg.get("url"):
        read, write = await stack.enter_async_context(streamable_http_client(str(cfg["url"])))
    else:
        raise ValueError(f"MCP server config needs 'command' or 'url': {cfg!r}")
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session, stack
