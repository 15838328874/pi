"""web_search tool: URL construction, auth, formatting, and the SSRF-safety
invariant (fixed endpoint, no URL argument)."""

from __future__ import annotations

import asyncio

from pi.tools.base import ToolContext
from pi.tools.web_search import WebSearchTool


class _FakeResponse:
    def __init__(self, status_code: int = 200, body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._body = body or {}
        self.text = text

    def json(self) -> dict:
        return self._body


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url: str, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self._response


def _run(tool: WebSearchTool, args: dict):
    return asyncio.run(tool.execute(args, ToolContext()))


def test_formats_results_and_builds_fixed_url(monkeypatch):
    resp = _FakeResponse(
        200,
        body={
            "result": {
                "search_result": [
                    {
                        "title": "杭州天气",
                        "link": "https://x.com/t",
                        "snippet": "晴 20℃",
                        "meta_info": {"publishedTime": "2026-10-03T00:00:00Z"},
                    }
                ]
            }
        },
    )
    fake = _FakeClient(resp)
    monkeypatch.setattr("pi.tools.web_search.httpx.AsyncClient", lambda **kw: fake)

    tool = WebSearchTool(endpoint="http://search.example", api_key="OS-test")
    out = _run(tool, {"query": "杭州天气", "top_k": 3})

    assert not out.is_error
    assert "杭州天气" in out.content
    assert "https://x.com/t" in out.content
    assert "晴 20℃" in out.content

    call = fake.calls[0]
    assert call["url"] == (
        "http://search.example/v3/openapi/workspaces/default/web-search/ops-web-search-001"
    )
    assert call["json"]["query"] == "杭州天气"
    assert call["json"]["top_k"] == 3
    assert call["headers"]["Authorization"] == "Bearer OS-test"


def test_clamps_top_k(monkeypatch):
    fake = _FakeClient(_FakeResponse(200, body={"result": {"search_result": []}}))
    monkeypatch.setattr("pi.tools.web_search.httpx.AsyncClient", lambda **kw: fake)
    tool = WebSearchTool(endpoint="http://s", api_key="k")
    _run(tool, {"query": "q", "top_k": 9999})
    assert fake.calls[0]["json"]["top_k"] == 10  # MAX_K


def test_requires_config(monkeypatch):
    monkeypatch.delenv("PI_WEB_SEARCH_HOST", raising=False)
    monkeypatch.delenv("PI_WEB_SEARCH_API_KEY", raising=False)
    tool = WebSearchTool()  # no endpoint/key injected, env cleared
    out = _run(tool, {"query": "q"})
    assert out.is_error
    assert "not configured" in out.content


def test_http_error_is_surfaced(monkeypatch):
    fake = _FakeClient(_FakeResponse(500, text="boom"))
    monkeypatch.setattr("pi.tools.web_search.httpx.AsyncClient", lambda **kw: fake)
    tool = WebSearchTool(endpoint="http://s", api_key="k")
    out = _run(tool, {"query": "q"})
    assert out.is_error
    assert "HTTP 500" in out.content


def test_error_body_with_http_200_is_surfaced(monkeypatch):
    fake = _FakeClient(
        _FakeResponse(200, body={"code": "InvalidParameter", "message": "bad query"})
    )
    monkeypatch.setattr("pi.tools.web_search.httpx.AsyncClient", lambda **kw: fake)
    tool = WebSearchTool(endpoint="http://s", api_key="k")
    out = _run(tool, {"query": "q"})
    assert out.is_error
    assert "InvalidParameter" in out.content
