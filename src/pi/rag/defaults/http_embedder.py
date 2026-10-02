"""HTTP embedder - supports BOTH DashScope-native and OpenAI-compatible wires.

Two shapes exist in the wild for the same vendor (Aliyun MaaS):

  dashscope (native)                      openai (compatible-mode)
  POST .../api/v1/services/embeddings     POST .../compatible-mode/v1/embeddings
  {"model": m, "input": {"texts": [...]}} {"model": m, "input": [...]}
  -> {"output":{"embeddings":[             -> {"data":[{"index":i,
       {"text_index":i,"embedding":[..]}],       "embedding":[..]}],
     "usage":{"total_tokens":N}}             "usage":{"prompt_tokens":N,
                                                     "total_tokens":N}}

pi.llm.embedding.EmbeddingClient implements the native shape; this module
covers both so standalone users can point at either endpoint. The pi
integration keeps using pi's own client via adapters.py - the kernel never
imports it (portability rule).

Style selection: ``style="openai"|"dashscope"|"auto"``. auto probes by URL
(`/compatible-mode/` -> openai, else dashscope) - explicit config wins.

trust_env=False: dead http(s)_proxy env vars must not hijack outbound calls
(same house rule as EmbeddingClient / MilvusStore grpc proxy kill).

Retries: transient failures (connection errors, 429, 5xx) are retried with
jittered exponential backoff - see ``defaults/_http.py`` for what is NOT retried
(a slow endpoint is not a flaky one). The pooled client is created once and
reused, so a retry costs no extra TCP/TLS handshake.
"""

from __future__ import annotations

import logging

import httpx

from pi.rag.defaults._http import post_json_with_retries
from pi.rag.types import EmbedResult

log = logging.getLogger("pi.rag.defaults.http_embedder")

TIMEOUT = 30.0
# Retries default to 2 (not 0): the vector channel is the only semantic path, so
# a transient 429/502 that ends in a degradation costs real answer quality. Only
# transient failures are retried - see defaults/_http.py for the policy.
RETRIES = 2
RETRY_BACKOFF_S = 0.5

STYLE_OPENAI = "openai"
STYLE_DASHSCOPE = "dashscope"


class EmbeddingError(Exception):
    """Raised on any embedding failure; the retriever logs-and-degrades."""


def _infer_style(url: str) -> str:
    """auto: /compatible-mode/ in the path means the OpenAI wire."""
    return STYLE_OPENAI if "/compatible-mode" in url else STYLE_DASHSCOPE


class HttpEmbedder:
    def __init__(
        self,
        url: str,
        api_key: str,
        model: str,
        timeout: float = TIMEOUT,
        batch_size: int = 16,
        style: str = "auto",
        retries: int = RETRIES,
        retry_backoff_s: float = RETRY_BACKOFF_S,
        transport: httpx.AsyncBaseTransport | None = None,  # test seam (mock transport)
    ) -> None:
        if style not in ("auto", STYLE_OPENAI, STYLE_DASHSCOPE):
            raise ValueError(f"unknown embedding style {style!r}")
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.batch_size = max(1, batch_size)
        self.attempts = 1 + max(0, int(retries))
        self.retry_backoff_s = max(0.0, float(retry_backoff_s))
        self.style = _infer_style(self.url) if style == "auto" else style
        self._transport = transport
        # Shared connection pool (R8): one AsyncClient for the embedder's
        # lifetime instead of a fresh TCP+TLS handshake per batch. Created
        # lazily so a never-used instance (tests) holds no open resources.
        # The OWNER is the runtime that constructed this embedder - it must
        # call aclose() on teardown (adapters.RagRuntime does). Named _pool:
        # a `_client` ATTRIBUTE would shadow the _client() method.
        self._pool: httpx.AsyncClient | None = None

    async def embed(self, texts: list[str]) -> EmbedResult:
        """Embed a batch (auto-split by batch_size); vectors in input order."""
        if not texts:
            return EmbedResult(vectors=[], usage_tokens=0)
        vectors: list[list[float]] = []
        usage = 0
        for i in range(0, len(texts), self.batch_size):
            part = await self._embed_batch(texts[i : i + self.batch_size])
            vectors.extend(part.vectors)
            usage += part.usage_tokens
        return EmbedResult(vectors=vectors, usage_tokens=usage)

    async def embed_query(self, text: str) -> EmbedResult:
        return await self._embed_batch([text])

    def _client(self) -> httpx.AsyncClient:
        """The shared pooled client; created on first use (lazy).

        trust_env=False stays: dead http(s)_proxy env vars must not hijack
        outbound calls (house rule). The OWNER is whoever constructed this
        embedder (the runtime) - they call aclose() on teardown.
        """
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

    async def _embed_batch(self, texts: list[str]) -> EmbedResult:
        if self.style == STYLE_OPENAI:
            body: dict = {"model": self.model, "input": texts}
        else:
            body = {"model": self.model, "input": {"texts": texts}}
        try:
            client = self._client()
            resp = await post_json_with_retries(
                client,
                self.url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                payload=body,
                attempts=self.attempts,
                base_backoff_s=self.retry_backoff_s,
                label="rag embedding",
            )
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embedding request failed: {exc}") from exc
        if resp.status_code != 200:
            # Surface the vendor error body: invalid_api_key etc. are the #1
            # support question and the raw message saves a round trip.
            detail = resp.text[:300]
            raise EmbeddingError(f"embedding endpoint returned HTTP {resp.status_code}: {detail}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise EmbeddingError(f"embedding response is not JSON: {exc}") from exc

        if self.style == STYLE_OPENAI:
            return self._parse_openai(payload, len(texts))
        return self._parse_dashscope(payload, len(texts))

    @staticmethod
    def _validate_vec(vec: object) -> None:
        if not isinstance(vec, list) or not vec:
            raise EmbeddingError("embedding entry is empty or not a list")
        if not all(isinstance(x, (int, float)) for x in vec):
            raise EmbeddingError("embedding entry is not a list of floats")

    def _parse_openai(self, payload: dict, n: int) -> EmbedResult:
        try:
            entries = payload["data"]
        except (KeyError, TypeError) as exc:
            # OpenAI-compatible endpoints put auth/model errors under "error".
            err = payload.get("error") if isinstance(payload, dict) else None
            msg = err.get("message") if isinstance(err, dict) else str(payload)[:300]
            raise EmbeddingError(f"malformed openai embedding response: {msg}") from exc
        if not isinstance(entries, list) or len(entries) != n:
            raise EmbeddingError(
                f"embedding count mismatch: asked {n}, got {len(entries) if isinstance(entries, list) else '?'}"
            )
        # Defensive: order by `index` instead of trusting response order.
        by_index: dict[int, list[float]] = {}
        for entry in entries:
            try:
                vec = entry["embedding"]
                idx = entry.get("index", len(by_index))
            except (KeyError, TypeError) as exc:
                raise EmbeddingError(f"malformed openai embedding entry: {exc}") from exc
            self._validate_vec(vec)
            by_index[int(idx)] = list(vec)
        if len(by_index) != n:
            raise EmbeddingError("openai embedding response has duplicate/missing indices")
        usage = payload.get("usage") or {}
        tokens = usage.get("total_tokens") or usage.get("prompt_tokens") or 0
        return EmbedResult(vectors=[by_index[i] for i in range(n)], usage_tokens=int(tokens))

    def _parse_dashscope(self, payload: dict, n: int) -> EmbedResult:
        try:
            entries = payload["output"]["embeddings"]
        except (KeyError, TypeError) as exc:
            raise EmbeddingError(f"malformed dashscope embedding response: {payload}") from exc
        if len(entries) != n:
            raise EmbeddingError(f"embedding count mismatch: asked {n}, got {len(entries)}")
        by_index: dict[int, list[float]] = {}
        for entry in entries:
            try:
                vec = entry["embedding"]
                idx = entry.get("text_index", len(by_index))
            except (KeyError, TypeError) as exc:
                raise EmbeddingError(f"malformed embedding entry: {exc}") from exc
            self._validate_vec(vec)
            by_index[int(idx)] = list(vec)
        if len(by_index) != n:
            raise EmbeddingError("dashscope embedding response missing text_index entries")
        usage = (payload.get("usage") or {}).get("total_tokens", 0)
        return EmbedResult(vectors=[by_index[i] for i in range(n)], usage_tokens=int(usage or 0))
