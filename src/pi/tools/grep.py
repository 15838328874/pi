"""Regex grep across the workspace tree."""

from __future__ import annotations

import os
from typing import Any

import regex as re

from pi.tools.base import Tool, ToolContext, ToolResult, get_fs, resolve_path

MAX_MATCHES = 200
MAX_FILE_BYTES = 1_000_000
# Per-match time budget (seconds). The `regex` module interrupts catastrophic
# backtracking (e.g. (a+)+$ on a long non-match) by raising TimeoutError, which
# stdlib `re` cannot do. LLM-supplied patterns must not be able to hang the
# event loop / worker thread indefinitely.
REGEX_TIMEOUT = 0.5


class GrepTool(Tool):
    name = "grep"
    capabilities = frozenset({"filesystem.read"})
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
        fs = get_fs(ctx)
        if not await fs.exists(root):
            return ToolResult(content=f"Error: path not found: {args.get('path')}", is_error=True)

        matches: list[str] = []
        files_scanned = 0
        truncated = False

        if not await fs.is_dir(root):
            candidates = [root]
            base_dir = root.parent
        else:
            candidates = [p for p in await fs.walk(root) if not await fs.is_dir(p)]
            base_dir = root

        for filepath in candidates:
            if len(matches) >= MAX_MATCHES:
                truncated = True
                break
            fname = filepath.name
            if include and not _glob_match(include, fname):
                continue
            try:
                size = await fs.file_size(filepath)
                if size is None or size > MAX_FILE_BYTES:
                    continue
                data = await fs.read_bytes(filepath)
                if data is None:
                    continue
            except Exception:  # noqa: BLE001 - keep scanning past bad files
                continue
            if b"\x00" in data[:4096]:
                continue
            files_scanned += 1
            rel = str(filepath.relative_to(base_dir)).replace("\\", "/")
            try:
                for lineno, line in enumerate(data.decode("utf-8", errors="ignore").splitlines(), 1):
                    if regex.search(line, timeout=REGEX_TIMEOUT):
                        matches.append(f"{rel}:{lineno}: {line.strip()[:400]}")
                        if len(matches) >= MAX_MATCHES:
                            truncated = True
                            break
            except TimeoutError:
                # Catastrophic backtracking on this file: stop scanning its
                # remaining lines (further matches would likely time out too)
                # and move on to the next candidate.
                continue

        if not matches:
            return ToolResult(content=f"No matches for /{pattern}/ ({files_scanned} files scanned)")
        suffix = f"\n... (truncated at {MAX_MATCHES} matches)" if truncated else ""
        return ToolResult(content="\n".join(matches) + suffix)


def _glob_match(pattern: str, name: str) -> bool:
    from fnmatch import fnmatch

    return fnmatch(name, pattern)
