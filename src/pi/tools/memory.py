"""Semantic memory tools: remember / recall (cross-session long-term memory).

These are the write/read surface of the semantic memory layer. The runner also
injects relevant memories into the system prompt automatically at turn start, so
recall is a fallback for when the agent needs a specific lookup.
"""

from __future__ import annotations

from typing import Any

from pi.tools.base import Tool, ToolContext, ToolResult


class RememberTool(Tool):
    name = "remember"
    description = (
        "Save a fact, preference, or decision to long-term memory. It persists "
        "across sessions, so use it for things you (or the user) will need later."
    )
    input_schema = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "the note to remember"}},
        "required": ["text"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if getattr(ctx, "memory", None) is None:
            return ToolResult(content="Error: memory store is not configured", is_error=True)
        text = str(args.get("text", "")).strip()
        if not text:
            return ToolResult(content="Error: `text` is required", is_error=True)
        await ctx.memory.add(getattr(ctx, "user_db_id", 0), text)
        return ToolResult(content="remembered")


class RecallTool(Tool):
    name = "recall"
    description = (
        "Search long-term memory for notes relevant to a query. Returns the most "
        "relevant saved facts (already injected automatically at turn start, so "
        "use this only for a targeted lookup)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "what to search for"},
            "k": {"type": "integer", "description": "max results (default 3)"},
        },
        "required": ["query"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.memory is None:
            return ToolResult(content="Error: memory store is not configured", is_error=True)
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(content="Error: `query` is required", is_error=True)
        k = int(args.get("k", 3))
        rows = await ctx.memory.search(getattr(ctx, "user_db_id", 0), query, k)
        if not rows:
            return ToolResult(content="(no relevant memories)")
        return ToolResult(content="\n".join(f"- {r.text}" for r in rows))
