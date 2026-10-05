"""Write a file (creates parent directories)."""

from __future__ import annotations

from typing import Any

from pi.tools.base import Tool, ToolContext, ToolResult, ensure_fs, resolve_path, workspace_size


class WriteTool(Tool):
    name = "write"
    capabilities = frozenset({"filesystem.write"})
    description = (
        "Write content to a file in the workspace, creating parent directories as needed. "
        "Use a RELATIVE path (e.g. 'out.txt'); the sandbox's /workspace is this same directory "
        "bind-mounted, so writing 'out.txt' creates /workspace/out.txt inside the sandbox. "
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
        fs = await ensure_fs(ctx)
        if await fs.is_dir(path):
            return ToolResult(content=f"Error: {raw} is a directory", is_error=True)

        # 每会话 workspace 磁盘配额：写前硬拦。覆盖旧文件时按「净增长」算
        # （当前总大小 - 旧文件大小 + 新内容），避免把覆盖误判成新增。
        if ctx.workspace_max_bytes > 0:
            new_bytes = len(content.encode("utf-8"))
            old_bytes = 0
            try:
                old_bytes = await fs.file_size(path) or 0  # None（不存在）→ 0
            except Exception:  # noqa: BLE001 - size probe is best-effort
                old_bytes = 0
            current = workspace_size(ctx.cwd)
            if current - old_bytes + new_bytes > ctx.workspace_max_bytes:
                cap = ctx.workspace_max_bytes / (1024 * 1024)
                return ToolResult(
                    content=(
                        f"Error: workspace quota exceeded ({cap:.0f}MB). "
                        f"Current workspace is {current / 1e6:.1f}MB. "
                        f"Delete or shrink existing files first, or write a smaller file."
                    ),
                    is_error=True,
                )

        try:
            await fs.write_bytes(path, content.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - surface any fs error uniformly
            return ToolResult(content=f"Error: cannot write {raw}: {exc}", is_error=True)

        return ToolResult(content=f"Wrote {len(content)} chars to {path}")
