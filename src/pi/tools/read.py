"""Read a file with line numbers."""

from __future__ import annotations

from typing import Any

from pi.tools.base import Tool, ToolContext, ToolResult, resolve_path, truncate

MAX_LINES = 2000
MAX_LINE_LEN = 2000


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:4096]


class ReadTool(Tool):
    name = "read"
    description = (
        "Read a text file, returned with 6-width line numbers (1-based). "
        "Use offset/limit for paging through large files. Binary files are rejected."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path, relative to cwd or absolute."},
            "offset": {
                "type": "integer",
                "description": "First line to return (1-based, default 1).",
                "default": 1,
            },
            "limit": {
                "type": "integer",
                "description": "Max number of lines to return (default 2000).",
                "default": 2000,
            },
        },
        "required": ["path"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw = str(args.get("path", "")).strip()
        if not raw:
            return ToolResult(content="Error: path is required", is_error=True)
        path = resolve_path(ctx, raw)

        if not path.exists():
            return ToolResult(content=f"Error: file not found: {raw}", is_error=True)
        if path.is_dir():
            return ToolResult(content=f"Error: {raw} is a directory (use ls)", is_error=True)

        data = path.read_bytes()
        if _looks_binary(data):
            return ToolResult(content=f"Error: {raw} looks like a binary file", is_error=True)

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")

        lines = text.splitlines()
        offset = max(1, int(args.get("offset", 1) or 1))
        limit = max(1, int(args.get("limit", MAX_LINES) or MAX_LINES))
        selected = lines[offset - 1 : offset - 1 + limit]

        numbered = []
        for i, line in enumerate(selected, start=offset):
            numbered.append(f"{i:>6}\t{line[:MAX_LINE_LEN]}")
        body = "\n".join(numbered) if numbered else "(empty range)"

        total_note = (
            f"\n(showing lines {offset}-{offset + len(selected) - 1} of {len(lines)})"
            if len(lines) > len(selected)
            else ""
        )
        return ToolResult(content=truncate(body + total_note, ctx.max_output))
