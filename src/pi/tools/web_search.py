"""web_search: live internet search via Aliyun OpenSearch 联网搜索 API.

A normal builtin tool, so it flows through the same policy gate / audit / sandbox
/ tracing as every other tool (unlike the model-endpoint "builtin tools" idea,
which bypassed local governance by running provider-side).

SSRF discipline (why this is safe where the old web_fetch/web_search were not):
the endpoint is FIXED from config (``PI_WEB_SEARCH_HOST``), never taken from the
model's args - the model supplies a *query string*, not a URL. On top of that we
pin the request with ``trust_env=False`` (no ambient proxy hijack) and
``follow_redirects=False`` (a redirect to a private/metadata address cannot be
followed). There is no path for a model to steer this request to a host it
chooses.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from pi.tools.base import Tool, ToolContext, ToolResult, truncate

log = logging.getLogger("pi.tools.web_search")

# Cap regardless of what the model asks: results go straight into the context
# window, so an unbounded k is a self-inflicted context blowup.
MAX_K = 10
DEFAULT_K = 5

DEFAULT_WORKSPACE = "default"
DEFAULT_SERVICE_ID = "ops-web-search-001"

# content_type=snippet keeps the payload small; mainText pulls full page bodies
# (far heavier, rarely needed for a coding agent).
_CONTENT_TYPE = "snippet"


class WebSearchTool(Tool):
    name = "web_search"
    # A dedicated capability so operators can deny live web access independently
    # of internal knowledge retrieval (knowledge.retrieve = rag_search).
    capabilities = frozenset({"web.search"})
    description = (
        "Search the LIVE internet and return up-to-date web results with title, "
        "URL and snippet. Use this for current events, real-time facts, or any "
        "information not present in the user's uploaded documents. For internal / "
        "company knowledge use rag_search instead."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search the web for. Natural language works.",
            },
            "top_k": {
                "type": "integer",
                "description": f"Max results to return (default {DEFAULT_K}, max {MAX_K}).",
                "default": DEFAULT_K,
            },
        },
        "required": ["query"],
    }

    def __init__(self, *, endpoint: str | None = None, api_key: str | None = None) -> None:
        # Test / standalone injection seam; production reads env via _config().
        self._endpoint = endpoint
        self._api_key = api_key

    def _config(self) -> tuple[str, str]:
        host = (self._endpoint or os.environ.get("PI_WEB_SEARCH_HOST", "")).rstrip("/")
        key = self._api_key or os.environ.get("PI_WEB_SEARCH_API_KEY", "")
        return host, key

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(content="Error: `query` is required", is_error=True)

        host, key = self._config()
        if not host:
            return ToolResult(
                content="Error: web_search is not configured (PI_WEB_SEARCH_HOST unset)",
                is_error=True,
            )
        if not key:
            return ToolResult(
                content="Error: web_search is not configured (PI_WEB_SEARCH_API_KEY unset)",
                is_error=True,
            )

        try:
            top_k = int(args.get("top_k") or DEFAULT_K)
        except (TypeError, ValueError):
            top_k = DEFAULT_K
        top_k = max(1, min(top_k, MAX_K))

        workspace = os.environ.get("PI_WEB_SEARCH_WORKSPACE", DEFAULT_WORKSPACE)
        service_id = os.environ.get("PI_WEB_SEARCH_SERVICE_ID", DEFAULT_SERVICE_ID)
        url = f"{host}/v3/openapi/workspaces/{workspace}/web-search/{service_id}"

        payload = {
            "query": query,
            "top_k": top_k,
            "content_type": _CONTENT_TYPE,
            "query_rewrite": True,
        }

        try:
            async with httpx.AsyncClient(
                timeout=15.0, trust_env=False, follow_redirects=False
            ) as client:
                resp = await client.post(
                    url,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {key}",
                    },
                )
        except Exception as exc:  # noqa: BLE001 - a search failure must not kill the run
            log.exception("web_search request failed")
            return ToolResult(
                content=f"Error: web search failed ({type(exc).__name__}: {exc})",
                is_error=True,
            )

        if resp.status_code != 200:
            return ToolResult(
                content=(
                    f"Error: web search returned HTTP {resp.status_code}: "
                    f"{resp.text[:300]}"
                ),
                is_error=True,
            )

        try:
            data = resp.json()
        except json.JSONDecodeError:
            return ToolResult(content="Error: web search returned non-JSON", is_error=True)

        # The API reports some errors in-body with HTTP 200 (code/message).
        if data.get("code") and data.get("message"):
            return ToolResult(
                content=f"Error: web search: {data.get('code')}: {data.get('message')}",
                is_error=True,
            )

        results = (data.get("result") or {}).get("search_result") or []
        if not results:
            return ToolResult(content=f"No web results for: {query}")

        return ToolResult(content=truncate(_format(results, query), ctx.max_output))


def _format(results: list[dict[str, Any]], query: str) -> str:
    lines: list[str] = [f"{len(results)} web result(s) for: {query}", ""]
    for i, r in enumerate(results, 1):
        title = str(r.get("title") or "").strip()
        link = str(r.get("link") or "").strip()
        snippet = str(r.get("snippet") or "").strip()
        published = str((r.get("meta_info") or {}).get("publishedTime") or "").strip()
        lines.append(f"[{i}] {title or '(untitled)'}")
        if link:
            lines.append(f"    {link}")
        if published:
            lines.append(f"    published: {published}")
        if snippet:
            lines.append(f"    {snippet}")
        lines.append("")
    return "\n".join(lines).rstrip()
