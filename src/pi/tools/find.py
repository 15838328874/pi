"""Glob-based file finder."""

from __future__ import annotations

import os
from fnmatch import fnmatch
from typing import Any

from pi.tools.base import SKIP_DIRS, Tool, ToolContext, ToolResult, resolve_path

MAX_RESULTS = 500


class FindTool(Tool):
    name = "find"
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
        if not root.is_dir():
            return ToolResult(
                content=f"Error: directory not found: {args.get('path')}", is_error=True
            )

        hits: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
            for name in dirnames + filenames:
                rel = name if rel_dir == "." else f"{rel_dir}/{name}"
                if fnmatch(rel, pattern) or fnmatch(name, pattern):
                    hits.append(rel)
            if len(hits) >= MAX_RESULTS:
                break

        if not hits:
            return ToolResult(content=f"No files matching {pattern!r}")
        suffix = f"\n... (truncated at {MAX_RESULTS} results)" if len(hits) >= MAX_RESULTS else ""
        return ToolResult(content="\n".join(hits) + suffix)
