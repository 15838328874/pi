"""Run a shell command in the workspace cwd (optionally inside a sandbox)."""

from __future__ import annotations

from typing import Any

from pi.tools.base import Tool, ToolContext, ToolResult, truncate
from pi.tools.sandbox import LocalRunner


class BashTool(Tool):
    name = "bash"
    description = (
        "Run a shell command and return combined stdout/stderr plus the exit code. "
        "Use for builds, tests, git, and any inspection not covered by other tools. "
        "Prefer dedicated tools (read/write/edit/grep/find/ls) for file operations."
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

        runner = ctx.runner if ctx.runner is not None else LocalRunner()
        result = await runner.run(command, ctx.cwd, timeout)

        if result.exit_code != 0 and result.output.startswith("Error:"):
            return ToolResult(content=truncate(result.output, ctx.max_output), is_error=True)

        content = f"{result.output}\n\n(exit code: {result.exit_code})"
        return ToolResult(content=truncate(content, ctx.max_output))
