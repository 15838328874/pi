"""HTTP reranker (cross-encoder) - optional precision stage.

Wire contract follows the DashScope gte-rerank / TEI-compatible shape:
  POST {url}  Authorization: Bearer {key}
  {"model": ..., "input": {"query": q, "documents": [text, ...]},
   "parameters": {"top_n": n, "return_documents": false}}
  -> {"output": {"results": [{"index": i, "relevance_score": s}, ...]},
      "usage": {"total_tokens": N}}

Failure semantics (house rule): raise on any error; the retriever catches,
SKIPS rerank (keeps RRF order), and reports outcome=rerank_failed so the
degradation makes noise instead of silently changing answer quality.

Timeout: ``timeout`` is the per-attempt budget and is CONFIGURABLE
(``PI_RAG_RERANK_TIMEOUT``). It used to be a hardcoded 15s and that turned a
slow model into an all-requests-degraded incident whose symptom looked like
"the model is bad". A rerank batch is the single largest blocking call in a
query, so this is a knob operators must be able to move.

Retries: only transient transport/status failures, and never a ReadTimeout -
retrying a slow cross-encoder just doubles the time the user waits.
"""

from __future__ import annotations

import logging

import httpx

from pi.rag.defaults._http import post_json_with_retries
from pi.rag.types import EmbedResult, RetrievedChunk

log = logging.getLogger("pi.rag.defaults.http_reranker")

TIMEOUT = 15.0
# 1 retry by default: rerank failure degrades gracefully (fused order is kept),
# so the value of a retry is lower than for embedding - but a dropped connection
# or a 429 is free to retry, and that is all this budget can be spent on.
RETRIES = 1
RETRY_BACKOFF_S = 0.5


class RerankError(Exception):
    """Raised on any rerank failure; the retriever skips rerank and degrades."""


class HttpReranker:
    def __init__(
        self,
        url: str,
        api_key: str = "",
        model: str = "",
        timeout: float = TIMEOUT,
        retries: int = RETRIES,
        retry_backoff_s: float = RETRY_BACKOFF_S,
        transport: httpx.AsyncBaseTransport | None = None,  # test seam (mock transport)
    ) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.attempts = 1 + max(0, int(retries))
        self.retry_backoff_s = max(0.0, float(retry_backoff_s))
        self._transport = transport
        self.last_usage: EmbedResult | None = None  # retriever reads for metering
        # Shared connection pool (R8): one AsyncClient for the reranker's
        # lifetime instead of a fresh TCP+TLS handshake per request. Lazy so a
        # never-used instance holds no open resources; the runtime that built
        # this reranker calls aclose() on teardown. Named _pool: a `_client`
        # ATTRIBUTE would shadow the _client() method (met in review).
        self._pool: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._pool is None:
            self._pool = httpx.AsyncClient(
                timeout=self.timeout, trust_env=False, transport=self._transport
            )
        return self._pool

    async def aclose(self) -> None:
        """Release the pooled connections. The runtime teardown calls this."""
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None

    async def rerank(self, query: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
        if not chunks:
            return []
        # Channel symmetry: the vector channel embedded title_path (contextual
        # prefix) and BM25 indexed title_path+body. Handing the cross-encoder
        # the bare body asks it to re-score with LESS information than the
        # channels that built its candidate list had - it then cannot answer
        # heading-path queries and promotes same-document neighbours instead.
        # See RetrievedChunk.text_to_score().
        docs = [c.text_to_score() for c in chunks]
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            client = self._client()
            resp = await post_json_with_retries(
                client,
                self.url,
                headers=headers,
                payload={
                    "model": self.model,
                    "input": {"query": query, "documents": docs},
                    "parameters": {"top_n": len(docs), "return_documents": False},
                },
                attempts=self.attempts,
                base_backoff_s=self.retry_backoff_s,
                label="rag rerank",
            )
        except httpx.HTTPError as exc:
            raise RerankError(f"rerank request failed: {exc}") from exc
        if resp.status_code != 200:
            raise RerankError(f"rerank endpoint returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            # Parse ONCE: the body can be ~100KB for a 20-document batch and the
            # usage block was previously read by re-parsing the whole response.
            payload = resp.json()
            results = payload["output"]["results"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RerankError(f"malformed rerank response: {exc}") from exc

        scored: list[RetrievedChunk] = []
        for r in results:
            idx = int(r["index"])
            if not (0 <= idx < len(chunks)):
                raise RerankError(f"rerank result index {idx} out of range")
            # Copy, do NOT mutate the caller's chunk (retriever may reuse it
            # for citation assembly; in-place score edits leak across stages).
            src = chunks[idx]
            scored.append(
                RetrievedChunk(
                    chunk_id=src.chunk_id,
                    doc_key=src.doc_key,
                    text=src.text,
                    score=float(r.get("relevance_score", 0.0)),
                    title=src.title,
                    title_path=src.title_path,
                    source=src.source,
                    page=src.page,
                )
            )
        scored.sort(key=lambda c: c.score, reverse=True)
        usage = (payload.get("usage") or {}).get("total_tokens", 0)
        self.last_usage = EmbedResult(vectors=[], usage_tokens=int(usage or 0))
        return scored


class NoopHooks:
    """Default UsageHooks: swallow everything, count nothing.

    Standalone mode uses this; the pi adapters replace it with hooks wired to
    MemoryRepo.on_embed_usage / on_retrieval so RAG spend hits user quota.
    """

    def __init__(self) -> None:
        self.embed_tokens = 0
        self.outcomes: list[tuple[str, float]] = []

    async def on_embed_usage(self, user_id: int, tokens: int, kind: str = "embedding") -> None:
        self.embed_tokens += int(tokens or 0)

    async def on_retrieval(self, outcome: str, duration_s: float) -> None:
        self.outcomes.append((outcome, duration_s))
