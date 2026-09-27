"""Internet access tools: web_fetch and web_search (no API key required).

- web_fetch : GET a URL and convert HTML to readable plain text
- web_search: query DuckDuckGo's HTML endpoint (key-free, no scraping API needed)

Uses httpx (async) + stdlib html.parser — no BeautifulSoup dependency.
"""

from __future__ import annotations

import html as html_mod
import re
from html.parser import HTMLParser
from typing import Any

import httpx

from pi.tools.base import Tool, ToolContext, ToolResult, truncate

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 pi-py/0.1"
)
TIMEOUT = 30.0
MAX_RESULT_CHARS = 20_000

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head", "iframe"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3",
    "h4", "h5", "h6", "section", "article", "header", "footer", "nav",
    "aside", "blockquote", "pre", "hr",
}


class _TextExtractor(HTMLParser):
    """Minimal HTML -> plain text converter (stdlib only)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self.parts.append(data)


def html_to_text(content: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(content)
    except Exception:  # noqa: BLE001 - tolerate malformed HTML
        pass
    text = "".join(parser.parts)
    # collapse runs of blank lines and trailing spaces
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    collapsed: list[str] = []
    blank = 0
    for ln in lines:
        if ln:
            collapsed.append(ln)
            blank = 0
        else:
            blank += 1
            if blank == 1:
                collapsed.append("")
    return "\n".join(collapsed).strip()


def _extract_title(content: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", content, re.I | re.S)
    return html_mod.unescape(m.group(1)).strip()[:200] if m else ""


class WebFetchTool(Tool):
    name = "web_fetch"
    description = (
        "Fetch a URL over the internet and return its content as plain text "
        "(HTML is converted to readable text). Use to read web pages, docs, "
        "APIs, raw files. Follows up to 5 redirects. For finding pages first, use web_search."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full URL, e.g. https://example.com/page"},
        },
        "required": ["url"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = str(args.get("url", "")).strip()
        if not url:
            return ToolResult(content="Error: url is required", is_error=True)
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=TIMEOUT,
                headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            ) as client:
                resp = await client.get(url)
        except httpx.HTTPError as exc:
            return ToolResult(content=f"Error: fetch failed: {exc}", is_error=True)

        ctype = resp.headers.get("content-type", "")
        body = resp.text
        if "html" in ctype or body.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
            title = _extract_title(body)
            text = html_to_text(body)
            header = f"[fetched {resp.url} | HTTP {resp.status_code}"
            header += f" | title: {title}" if title else ""
            header += "]"
            content = f"{header}\n\n{text}"
        else:
            content = f"[fetched {resp.url} | HTTP {resp.status_code} | {ctype}]\n\n{body}"

        return ToolResult(content=truncate(content, MAX_RESULT_CHARS))


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the internet via DuckDuckGo (no API key needed) and return "
        "top results as 'title | url | snippet'. Use for finding current info, "
        "docs, packages, news. Then use web_fetch to read a specific page."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search keywords."},
            "max_results": {
                "type": "integer",
                "description": "Max results to return (default 8, max 20).",
                "default": 8,
            },
        },
        "required": ["query"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(content="Error: query is required", is_error=True)
        max_results = max(1, min(int(args.get("max_results", 8) or 8), 20))

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                resp = await client.post(
                    "https://duckduckgo.com/html/",
                    data={"q": query},
                )
        except httpx.HTTPError as exc:
            return ToolResult(content=f"Error: search failed: {exc}", is_error=True)

        if resp.status_code != 200:
            return ToolResult(
                content=f"Error: search endpoint returned HTTP {resp.status_code}",
                is_error=True,
            )

        results = _parse_ddg_results(resp.text, max_results)
        if not results:
            return ToolResult(content=f"No results for: {query}")

        lines = [f"{i}. {t}\n   {u}\n   {s}" for i, (t, u, s) in enumerate(results, 1)]
        return ToolResult(content="\n\n".join(lines))


def _parse_ddg_results(html_content: str, limit: int) -> list[tuple[str, str, str]]:
    """Parse DuckDuckGo HTML results page: (title, url, snippet)."""
    results: list[tuple[str, str, str]] = []
    # each result: <a class="result__a" href="...">title</a> ... <a class="result__snippet"...>snippet</a>
    link_re = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S
    )
    snippet_re = re.compile(
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.S
    )
    links = link_re.findall(html_content)
    snippets = snippet_re.findall(html_content)
    for i, (href, title_html) in enumerate(links):
        if i >= limit:
            break
        url = _ddg_unwrap_url(href)
        title = html_mod.unescape(re.sub(r"<[^>]+>", "", title_html)).strip()
        snippet = ""
        if i < len(snippets):
            snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", snippets[i])).strip()
        if title and url:
            results.append((title, url, snippet[:300]))
    return results


def _ddg_unwrap_url(href: str) -> str:
    """DuckDuckGo wraps links as //duckduckgo.com/l/?uddg=<encoded>; unwrap them."""
    href = html_mod.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    m = re.search(r"[?&]uddg=([^&]+)", href)
    if m:
        from urllib.parse import unquote

        return unquote(m.group(1))
    return href
