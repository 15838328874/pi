"""Glob-based file finder."""

from __future__ import annotations

import os
from fnmatch import fnmatch
from typing import Any

from pi.tools.base import Tool, ToolContext, ToolResult, get_fs, resolve_path

MAX_RESULTS = 500


class FindTool(Tool):
    name = "find"
    capabilities = frozenset({"filesystem.read"})
    description = (
        "Find files whose relative path matches a glob pattern (e.g. '*.py', "
        "'src/**/test_*'). Searches recursively from cwd or the given path. "
        "Use grep to search file contents."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern for the relative path."},
            "path": {
                "type": "string",
                "description": "Directory to search (default: cwd).",
            },
        },
        "required": ["pattern"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            return ToolResult(content="Error: pattern is required", is_error=True)

        root = resolve_path(ctx, str(args.get("path", ".")))
        fs = get_fs(ctx)
        if not await fs.exists(root) or not await fs.is_dir(root):
            return ToolResult(
                content=f"Error: directory not found: {args.get('path')}", is_error=True
            )

        hits: list[str] = []
        for full in await fs.walk(root):
            rel = str(full.relative_to(root)).replace("\\", "/")
            name = rel.rsplit("/", 1)[-1]
            if fnmatch(rel, pattern) or fnmatch(name, pattern):
                hits.append(rel)
            if len(hits) >= MAX_RESULTS:
                break

        if not hits:
            return ToolResult(content=f"No files matching {pattern!r}")
        suffix = f"\n... (truncated at {MAX_RESULTS} results)" if len(hits) >= MAX_RESULTS else ""
        return ToolResult(content="\n".join(hits) + suffix)
