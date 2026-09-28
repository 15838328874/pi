"""Run a shell command in the workspace cwd (optionally inside a sandbox)."""

from __future__ import annotations

from typing import Any

from pi.tools.base import Tool, ToolContext, ToolResult, truncate
from pi.tools.sandbox import LocalRunner


class BashTool(Tool):
    name = "bash"
    description = (
        "Run a shell command inside the sandbox and return stdout/stderr plus the exit code. "
        "The workspace is mounted at /workspace here — the SAME directory the file tools see, "
        "so /workspace/out.txt here is 'out.txt' to read/write/edit. Use for builds, tests, "
        "git, and inspecting files the sandbox produced. For reading/editing workspace files, "
        "prefer read/write/edit/grep/find/ls with RELATIVE paths (not /workspace/...)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 120, max 600).",
                "default": 120,
            },
        },
        "required": ["command"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult(content="Error: command is required", is_error=True)
        timeout = max(1, min(int(args.get("timeout", 120) or 120), 600))

        # 惰性沙箱：回合没建 VM 时，第一次 bash 调用现场创建（会话级池复用）。
        # 纯聊天回合上下文里 ensure_runner 为 None，永远走本地执行。
        if ctx.runner is None and ctx.ensure_runner is not None:
            await ctx.ensure_runner()
        runner = ctx.runner if ctx.runner is not None else LocalRunner()
        result = await runner.run(command, ctx.cwd, timeout)

        if result.exit_code != 0 and result.output.startswith("Error:"):
            return ToolResult(content=truncate(result.output, ctx.max_output), is_error=True)

        content = f"{result.output}\n\n(exit code: {result.exit_code})"
        return ToolResult(content=truncate(content, ctx.max_output))
