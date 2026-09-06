"""Write a file (creates parent directories)."""

from __future__ import annotations

from typing import Any

from pi.tools.base import Tool, ToolContext, ToolResult, resolve_path


class WriteTool(Tool):
    name = "write"
    description = (
        "Write content to a file, creating parent directories as needed. "
        "Overwrites the file if it exists. Use edit for partial changes."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path, relative to cwd or absolute."},
            "content": {"type": "string", "description": "Full file content to write."},
        },
        "required": ["path", "content"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw = str(args.get("path", "")).strip()
        content = args.get("content")
        if not raw:
            return ToolResult(content="Error: path is required", is_error=True)
        if content is None:
            return ToolResult(content="Error: content is required", is_error=True)
        if not isinstance(content, str):
            return ToolResult(content="Error: content must be a string", is_error=True)

        path = resolve_path(ctx, raw)
        if path.is_dir():
            return ToolResult(content=f"Error: {raw} is a directory", is_error=True)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(content=f"Error: cannot write {raw}: {exc}", is_error=True)

        return ToolResult(content=f"Wrote {len(content)} chars to {path}")
