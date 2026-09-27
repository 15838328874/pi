"""Regex grep across the workspace tree."""

from __future__ import annotations

import os
import re
from typing import Any

from pi.tools.base import SKIP_DIRS, Tool, ToolContext, ToolResult, resolve_path

MAX_MATCHES = 200
MAX_FILE_BYTES = 1_000_000


class GrepTool(Tool):
    name = "grep"
    description = (
        "Search file contents with a regular expression (Python re syntax), "
        "returning matches as 'path:line: text'. Searches recursively from cwd "
        "or the given path, skipping VCS/build dirs and binary files."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression to search for."},
            "path": {
                "type": "string",
                "description": "File or directory to search (default: cwd).",
            },
            "include": {
                "type": "string",
                "description": "Glob filter for file names, e.g. '*.py'.",
            },
        },
        "required": ["pattern"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            return ToolResult(content="Error: pattern is required", is_error=True)
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return ToolResult(content=f"Error: invalid regex: {exc}", is_error=True)

        include = args.get("include") or None
        root = resolve_path(ctx, str(args.get("path", ".")))
        if not root.exists():
            return ToolResult(content=f"Error: path not found: {args.get('path')}", is_error=True)

        matches: list[str] = []
        files_scanned = 0
        truncated = False

        if root.is_file():
            candidates = [root]
            base_dir = root.parent
        else:
            candidates = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for fn in filenames:
                    candidates.append(os.path.join(dirpath, fn))
            base_dir = root

        for filepath in candidates:
            if len(matches) >= MAX_MATCHES:
                truncated = True
                break
            fname = os.path.basename(filepath)
            if include and not _glob_match(include, fname):
                continue
            try:
                if os.path.getsize(filepath) > MAX_FILE_BYTES:
                    continue
                with open(filepath, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            if b"\x00" in data[:4096]:
                continue
            files_scanned += 1
            rel = os.path.relpath(filepath, base_dir).replace("\\", "/")
            for lineno, line in enumerate(data.decode("utf-8", errors="ignore").splitlines(), 1):
                if regex.search(line):
                    matches.append(f"{rel}:{lineno}: {line.strip()[:400]}")
                    if len(matches) >= MAX_MATCHES:
                        truncated = True
                        break

        if not matches:
            return ToolResult(content=f"No matches for /{pattern}/ ({files_scanned} files scanned)")
        suffix = f"\n... (truncated at {MAX_MATCHES} matches)" if truncated else ""
        return ToolResult(content="\n".join(matches) + suffix)


def _glob_match(pattern: str, name: str) -> bool:
    from fnmatch import fnmatch

    return fnmatch(name, pattern)
