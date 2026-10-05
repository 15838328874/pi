"""Directory listing."""

from __future__ import annotations

from typing import Any

from pi.tools.base import Tool, ToolContext, ToolResult, ensure_fs, resolve_path

MAX_ENTRIES = 500


class LsTool(Tool):
    name = "ls"
    capabilities = frozenset({"filesystem.read"})
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
        fs = await ensure_fs(ctx)
        if not await fs.exists(root):
            return ToolResult(content=f"Error: path not found: {args.get('path')}", is_error=True)
        if not await fs.is_dir(root):
            return ToolResult(content=f"Error: {args.get('path')} is a file (use read)", is_error=True)

        try:
            entries = await fs.list_dir(root)  # [(name, is_dir, size)]
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"Error: cannot list directory: {exc}", is_error=True)

        lines = []
        for name, is_dir, size in entries[:MAX_ENTRIES]:
            if is_dir:
                lines.append(f"{name}/")
            else:
                lines.append(f"{name}  ({_human(size)})")

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
