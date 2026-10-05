"""Exact-string edit with uniqueness guard (the pi/claude-code style edit tool)."""

from __future__ import annotations

import difflib
from typing import Any

from pi.tools.base import Tool, ToolContext, ToolResult, ensure_fs, resolve_path

MAX_DIFF_LINES = 60


class EditTool(Tool):
    name = "edit"
    capabilities = frozenset({"filesystem.write"})
    description = (
        "Replace an exact string in a workspace file (RELATIVE path; the sandbox's /workspace "
        "is this same directory bind-mounted). old_string must match exactly (including "
        "whitespace/indentation) and be unique in the file unless replace_all is true. "
        "Always read the file (or the relevant part) before editing."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path, relative to cwd or absolute."},
            "old_string": {"type": "string", "description": "Exact text to replace."},
            "new_string": {"type": "string", "description": "Replacement text."},
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence (default false).",
                "default": False,
            },
        },
        "required": ["path", "old_string", "new_string"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw = str(args.get("path", "")).strip()
        old = args.get("old_string")
        new = args.get("new_string")
        replace_all = bool(args.get("replace_all", False))

        if not raw:
            return ToolResult(content="Error: path is required", is_error=True)
        if not isinstance(old, str) or not isinstance(new, str):
            return ToolResult(
                content="Error: old_string and new_string are required strings", is_error=True
            )
        if old == new:
            return ToolResult(
                content="Error: old_string and new_string are identical", is_error=True
            )

        path = resolve_path(ctx, raw)
        fs = await ensure_fs(ctx)
        if not await fs.exists(path) or await fs.is_dir(path):
            return ToolResult(content=f"Error: file not found: {raw}", is_error=True)

        data = await fs.read_bytes(path)
        if data is None:
            return ToolResult(content=f"Error: file not found: {raw}", is_error=True)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(content=f"Error: {raw} is not valid UTF-8 text", is_error=True)

        count = text.count(old)
        if count == 0:
            return ToolResult(
                content=f"Error: old_string not found in {raw}. Read the file and retry with the exact text.",
                is_error=True,
            )
        if count > 1 and not replace_all:
            return ToolResult(
                content=(
                    f"Error: old_string occurs {count} times in {raw}. "
                    "Provide more surrounding context to make it unique, or set replace_all=true."
                ),
                is_error=True,
            )

        new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)

        try:
            await fs.write_bytes(path, new_text.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - uniform error surface
            return ToolResult(content=f"Error: cannot write {raw}: {exc}", is_error=True)

        diff = difflib.unified_diff(
            text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"{raw} (before)",
            tofile=f"{raw} (after)",
            n=2,
        )
        diff_text = "".join(diff)
        if len(diff_text.splitlines()) > MAX_DIFF_LINES:
            diff_text = "\n".join(diff_text.splitlines()[:MAX_DIFF_LINES]) + "\n... (diff truncated)"

        n_replaced = count if replace_all else 1
        return ToolResult(content=f"Replaced {n_replaced} occurrence(s) in {path}\n\n{diff_text}")
