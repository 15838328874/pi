"""rag_search: enterprise-document retrieval with citations (RagTool shell).

Thin tool by design (对接文档 §4.1): parse args -> call the injected retriever ->
format with sources. No HTTP here (the network boundary lives in the adapters /
defaults, and this keeps the repo's SSRF discipline intact), no SQL, no
embedding calls. Everything real is behind ``pi.rag.protocols``.

Two ways the retriever arrives, tried in order:
  - ``ctx.rag`` - per-turn injection. Kept as the FIRST choice so an agent that
    builds its own runtime can scope it per run (and so tests can pass a fake
    without touching the process).
  - ``pi.rag.adapters.get_runtime()`` - lazy process singleton. This is what the
    server uses: ``pi.rag.integration.install()`` publishes the runtime there at
    boot, so no ``ToolContext`` field and no runner plumbing are needed.

ACL is the whole point of ``ctx.user_db_id``: it is the INTEGER user id, and the
retriever pushes it into every channel (Milvus int filter, BM25 shard, SQL
WHERE) and re-verifies it at hydration. A missing user id is a hard error, never
an unscoped search - silently searching everything would be a cross-tenant leak.

Degradation is surfaced to the MODEL, not just the logs: if the vector store is
down the retriever falls back to BM25 and this tool says so in the content, so
an answer built on a degraded index is visibly weaker rather than silently
presented as full-quality.
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any

from pi.tools.base import Tool, ToolContext, ToolResult, truncate

log = logging.getLogger("pi.tools.rag")

# Cap on results regardless of what the model asks for: retrieval results go
# straight into the context window, and an unbounded k is a self-inflicted
# context blowup (the same reason web_search caps at 20).
MAX_K = 20
DEFAULT_K = 5


def _search_takes_doc_keys(fn: Any) -> bool:
    """Does this retriever's ``search`` accept the 4th ``doc_keys`` argument?

    Inspected instead of "call it and retry on TypeError": a TypeError raised
    INSIDE a 4-arg search would otherwise be swallowed and the whole retrieval
    would run a second time - double the latency and, worse, double the metering
    hooks (the user gets billed twice for one question). Production retriever
    (``HybridRetriever.search``) and eval-runner-shaped fakes are both resolved
    from the signature, and an unknown callable is assumed to take doc_keys.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # builtins / C callables: assume the real one
        return True
    params = list(sig.parameters.values())
    if any(p.kind is p.VAR_POSITIONAL for p in params):
        return True
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if any(p.kind is p.KEYWORD_ONLY and p.name == "doc_keys" for p in params):
        return True
    return len(positional) >= 4


class RagTool(Tool):
    name = "rag_search"
    description = (
        "Search the user's ingested enterprise documents (PDF/Word/CSV/Markdown) "
        "and return the most relevant passages WITH citations (doc, section, "
        "page). Use this instead of web_search for internal/company knowledge, "
        "policies, specs, or any document the user uploaded. Each result is "
        "quoted from a specific chunk - cite the source when you use it."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look for. Natural language works; exact terms work too.",
            },
            "k": {
                "type": "integer",
                "description": f"Max passages to return (default {DEFAULT_K}, max {MAX_K}).",
                "default": DEFAULT_K,
            },
            "filter_doc": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional: restrict the search to these doc keys (as returned in "
                    "previous results' `doc_id`). Omit to search all of the user's docs."
                ),
            },
        },
        "required": ["query"],
    }

    def __init__(self, retriever: Any = None) -> None:
        """``retriever`` is a test/standalone injection seam (anything with an
        async ``search(user_id, query, k, doc_keys)``). Production leaves it None
        and the runtime is resolved per call from ctx / the adapters singleton.
        """
        self._retriever = retriever

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(content="Error: `query` is required", is_error=True)

        # ACL gate: no integer user id -> refuse. Searching unscoped would leak
        # other tenants' documents, and "returned nothing" is not evidence of
        # safety (对接文档 §9 #5: prove the filter path ran).
        uid = ctx.user_db_id
        if uid is None:
            return ToolResult(
                content=(
                    "Error: rag_search requires an authenticated user (no user_db_id in "
                    "context). Document retrieval is per-user and cannot run unscoped."
                ),
                is_error=True,
            )

        try:
            k = int(args.get("k") or DEFAULT_K)
        except (TypeError, ValueError):
            k = DEFAULT_K
        k = max(1, min(k, MAX_K))

        doc_keys = _doc_keys(args.get("filter_doc"))

        retriever = self._retriever
        if retriever is None:
            injected = getattr(ctx, "rag", None)
            # ctx.rag may be the assembled RagRuntime (server path) or a bare
            # retriever (tests / minimal wiring); accept both.
            retriever = getattr(injected, "retriever", injected)
        if retriever is None:
            # Lazy process singleton (CLI / standalone). Import is local so that
            # importing pi.tools never drags in pymilvus / sqlalchemy.
            try:
                from pi.rag.adapters import get_runtime

                runtime = await get_runtime()
                retriever = runtime.retriever
            except Exception as exc:  # noqa: BLE001 - a broken RAG must not kill the run
                log.exception("rag runtime unavailable")
                return ToolResult(
                    content=f"Error: document retrieval is unavailable: {type(exc).__name__}: {exc}",
                    is_error=True,
                )

        search = getattr(retriever, "search", None)
        if not callable(search):
            return ToolResult(
                content="Error: injected rag retriever has no async search()", is_error=True
            )

        try:
            if _search_takes_doc_keys(search):
                result = await search(int(uid), query, k, doc_keys)
            else:
                # Injected fake with the eval-runner signature (user_id, query, k).
                result = await search(int(uid), query, k)
        except Exception as exc:  # noqa: BLE001 - retrieval failure degrades, never炸 the run
            log.exception("rag_search failed (user=%s)", uid)
            return ToolResult(
                content=(
                    f"Error: document retrieval failed ({type(exc).__name__}: {exc}). "
                    "The index may be unavailable; the answer must not pretend otherwise."
                ),
                is_error=True,
            )

        chunks = list(getattr(result, "chunks", None) or [])
        if not chunks:
            return ToolResult(content=_empty_notice(query, result, doc_keys))

        return ToolResult(content=truncate(_format(chunks, result, query), ctx.max_output))


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------


def _doc_keys(raw: Any) -> list[str] | None:
    """Normalise filter_doc: accept a list, a comma string, or nothing."""
    if raw is None:
        return None
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        parts = [str(p).strip() for p in raw]
    else:
        return None
    out = [p for p in parts if p]
    return out or None


def _citation(hit: Any) -> str:
    """One human/LLM-readable citation line: title > section (page, doc)."""
    title = str(getattr(hit, "title", "") or "").strip()
    path = str(getattr(hit, "title_path", "") or "").strip()
    head = path or title or "(untitled)"
    page = getattr(hit, "page", None)
    where = f" p.{page}" if page else ""
    source = str(getattr(hit, "source", "") or "").strip()
    doc = str(getattr(hit, "doc_key", "") or "").strip()
    origin = source or doc
    return f"{head}{where}" + (f" — {origin}" if origin else "")


def _format(chunks: list[Any], result: Any, query: str) -> str:
    """Cited passages + a machine-readable trailer.

    The trailer exists because the task book (§4.1) requires a structured
    ``{"chunks": [{doc_id, chunk_id, text, score, source}]}`` shape, and this
    branch's ``ToolResult`` has no ``payload`` field - so the structure rides in
    the content. Keeping it at the END means the model reads the prose first
    (what it will quote) and the JSON is available for anything that parses it.
    """
    lines: list[str] = [f"{len(chunks)} passage(s) for: {query}", ""]
    for i, hit in enumerate(chunks, 1):
        score = float(getattr(hit, "score", 0.0) or 0.0)
        text = str(getattr(hit, "text", "") or "").strip()
        lines.append(f"[{i}] {_citation(hit)}  (score {score:.4f})")
        lines.append(text)
        lines.append("")

    mode = str(getattr(getattr(result, "mode", None), "value", "") or "")
    outcome = str(getattr(result, "outcome", "") or "")
    degraded = bool(getattr(result, "degraded", False))
    duration_ms = int(getattr(result, "duration_ms", 0) or 0)
    if degraded:
        # Non-silent degradation, surfaced where it changes the answer: a
        # BM25/SQL-only result has no semantic matching, so paraphrased queries
        # may have missed relevant passages. The model should hedge, not bluff.
        lines.append(
            f"⚠ degraded retrieval (mode={mode or 'unknown'}, outcome={outcome or 'unknown'}): "
            "the semantic vector index was unavailable, so these results came from "
            "keyword/lexical matching only. Relevant passages phrased differently "
            "from the query may be missing - say so rather than asserting completeness."
        )
        lines.append("")

    payload = {
        "mode": mode,
        "outcome": outcome,
        "degraded": degraded,
        "duration_ms": duration_ms,
        "count": len(chunks),
        "chunks": [
            {
                "doc_id": str(getattr(h, "doc_key", "") or ""),
                "chunk_id": int(getattr(h, "chunk_id", 0) or 0),
                "title": str(getattr(h, "title", "") or ""),
                "title_path": str(getattr(h, "title_path", "") or ""),
                "source": str(getattr(h, "source", "") or ""),
                "page": getattr(h, "page", None),
                "score": round(float(getattr(h, "score", 0.0) or 0.0), 6),
                "text": str(getattr(h, "text", "") or ""),
            }
            for h in chunks
        ],
    }
    lines.append("```json")
    lines.append(json.dumps(payload, ensure_ascii=False))
    lines.append("```")
    return "\n".join(lines)


def _empty_notice(query: str, result: Any, doc_keys: list[str] | None) -> str:
    """Distinguish 'no such content' from 'index unavailable' - conflating them
    is how a dead vector store turns into confidently wrong answers."""
    mode = str(getattr(getattr(result, "mode", None), "value", "") or "")
    outcome = str(getattr(result, "outcome", "") or "")
    scope = f" (restricted to {len(doc_keys)} doc(s))" if doc_keys else ""
    if outcome in ("embed_failed", "sql_fallback", "error") or mode in ("sql_fallback", "empty"):
        return (
            f"No passages found for: {query}{scope}\n"
            f"⚠ retrieval was degraded (mode={mode or 'unknown'}, outcome={outcome or 'unknown'}), "
            "so this empty result may reflect an unavailable index rather than absent "
            "content. Do not conclude the documents lack the answer."
        )
    return (
        f"No passages found for: {query}{scope}\n"
        "Either the user has no ingested documents matching this, or the terms are "
        "too narrow - try rephrasing, or check whether the document was ingested."
    )
