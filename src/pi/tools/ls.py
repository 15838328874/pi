"""Directory listing."""

from __future__ import annotations

from typing import Any

from pi.tools.base import Tool, ToolContext, ToolResult, resolve_path

MAX_ENTRIES = 500


class LsTool(Tool):
    name = "ls"
    description = "List a directory: directories first (trailing '/'), then files with sizes."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory to list (default: cwd).",
            },
        },
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = resolve_path(ctx, str(args.get("path", ".")))
        if not root.exists():
            return ToolResult(content=f"Error: path not found: {args.get('path')}", is_error=True)
        if not root.is_dir():
            return ToolResult(content=f"Error: {args.get('path')} is a file (use read)", is_error=True)

        try:
            entries = sorted(root.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        except OSError as exc:
            return ToolResult(content=f"Error: cannot list directory: {exc}", is_error=True)

        lines = []
        for entry in entries[:MAX_ENTRIES]:
            try:
                if entry.is_dir():
                    lines.append(f"{entry.name}/")
                else:
                    size = entry.stat().st_size
                    lines.append(f"{entry.name}  ({_human(size)})")
            except OSError:
                lines.append(f"{entry.name}  (unavailable)")

        if not lines:
            return ToolResult(content="(empty directory)")
        suffix = ""
        if len(entries) > MAX_ENTRIES:
            suffix = f"\n... (truncated at {MAX_ENTRIES} entries)"
        return ToolResult(content="\n".join(lines) + suffix)


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"
