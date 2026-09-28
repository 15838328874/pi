"""File-pipeline tools: list uploaded files and pull them into the sandbox.

These tools query the MySQL `files` index and stage objects ON DEMAND. Dynamic
data (file list, presigned URLs) flows through tool RESULTS — never into
system_prompt — so the prompt prefix stays byte-stable across turns and the
session's KV cache survives (no per-turn prompt mutation).

fetch_file stages bytes through the SERVER into the sandbox (option B): the
object's source of truth stays in MinIO, nothing lands on host disk, but the
bytes transit server memory because CubeSandbox VMs are NAT-isolated and cannot
reach the host's MinIO endpoint directly. Upload/download endpoints elsehwhere
remain presigned-direct.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pi.tools.base import LocalFS, Tool, ToolContext, ToolResult, truncate

_SAFE = re.compile(r"[^A-Za-z0-9._-]")
# Server-memory staging cap: staging the whole object into RAM must not OOM the
# host (~3.7G total). Larger files need streaming/presigned-direct-from-VM,
# which is tracked as a follow-up.
_MAX_FETCH_BYTES = 256 * 1024 * 1024  # 256 MiB


class ListFilesTool(Tool):
    name = "list_files"
    description = (
        "List files the current user has uploaded to object storage (id, name, "
        "size, upload time). Use fetch_file to pull one into the sandbox workspace."
    )
    input_schema = {"type": "object", "properties": {}, "required": []}

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.files is None or ctx.user_db_id is None:
            return ToolResult(content="Error: file store not available", is_error=True)
        rows = await ctx.files.list_for_user(ctx.user_db_id)
        if not rows:
            return ToolResult(content="(no files uploaded yet)")
        lines = [f"{r.id}\t{r.filename}\t{r.size}\t{r.created_at}" for r in rows]
        body = "id\tname\tsize\tuploaded_at\n" + "\n".join(lines)
        return ToolResult(content=truncate(body, ctx.max_output))


class FetchFileTool(Tool):
    name = "fetch_file"
    description = (
        "Pull an uploaded file into the sandbox workspace by its original filename "
        "(see list_files for its id). Returns the workspace path to process it. "
        "Files up to 256MB are staged from object storage into the sandbox."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "file_id": {"type": "integer", "description": "File id from list_files."},
        },
        "required": ["file_id"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.files is None or ctx.store is None or ctx.user_db_id is None:
            return ToolResult(content="Error: file store not configured", is_error=True)
        try:
            fid = int(args.get("file_id", 0))
        except (TypeError, ValueError):
            return ToolResult(content="Error: file_id must be an integer", is_error=True)

        row = await ctx.files.by_id(fid)
        if row is None or row.user_id != ctx.user_db_id:
            return ToolResult(content=f"Error: file {fid} not found", is_error=True)

        if row.size > _MAX_FETCH_BYTES:
            return ToolResult(
                content=(
                    f"Error: file is {row.size} bytes (> {_MAX_FETCH_BYTES}) - "
                    "too large to stage into the sandbox in memory; streaming "
                    "support is a follow-up."
                ),
                is_error=True,
            )

        # 懒建沙箱（同 bash）；无沙箱则降级写本地 workspace
        if ctx.runner is None and ctx.ensure_runner is not None:
            await ctx.ensure_runner()

        safe = _SAFE.sub("_", Path(row.filename).name) or "downloaded"

        # 写入路径一律用宿主路径（ctx.cwd/safe）：SandboxFS 会按 host_root 映射到
        # VM /workspace/safe；LocalFS（无沙箱）则写本地 workspace。绝不可写 VM 的
        # 绝对 /workspace/... —— 那会触发 SandboxFS 的 "path escapes workspace"。
        dest_host = ctx.cwd / safe
        fs = ctx.fs if ctx.fs is not None else LocalFS()

        # 真身从 MinIO 取出（内存中转，to_thread 由 get_bytes 内部处理）
        try:
            data = await ctx.store.get_bytes(row.object_key, row.bucket)
        except Exception as exc:  # noqa: BLE001 - object 可能已被删/暂不可达
            return ToolResult(content=f"Error: fetch from storage failed: {exc}", is_error=True)

        await fs.write_bytes(dest_host, data)
        if len(data) != row.size:
            return ToolResult(
                content=f"Error: size mismatch ({len(data)} != {row.size})", is_error=True
            )
        # 给模型的路径用 VM 视角（沙箱内 bash 看到的是 /workspace/...）
        model_path = f"/workspace/{safe}" if ctx.fs is not None else str(dest_host)
        return ToolResult(
            content=f"fetched {row.filename} ({len(data)} bytes) -> {model_path}. Use bash/read to process it."
        )