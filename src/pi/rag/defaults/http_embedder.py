"""HTTP embedder for any OpenAI-compatible ``/embeddings`` endpoint.

One wire only - the OpenAI shape:

  POST {url}   {"model": m, "input": ["...", ...]}
  -> {"data": [{"index": i, "embedding": [..]}, ...],
      "usage": {"prompt_tokens": N, "total_tokens": N}}

**Why only one.** 早先这里同时支持 DashScope 原生线与 OpenAI 兼容线，用
``style="openai"|"dashscope"|"auto"`` 选择（auto 靠 URL 里有没有
``/compatible-mode`` 推断）。那套设计换来的是"配错不报错"：style 与实际端点
形态不一致时，请求照样发出去、响应照样解析，只是向量语义悄悄漂移——检索质量
下降而日志干净，属于最难查的一类问题。原生线也没有带来任何本模块用得到的
能力（没用它的 ``text_type``/``dimension`` 等参数）。

OpenAI 的 ``/embeddings`` 是事实标准，阿里云 MaaS 的 ``/compatible-mode/v1``、
OpenAI 官方、DeepSeek、vLLM、Ollama、TEI 等都提供同一形状。所以这里只实现它：
**换 embedding 模型只需要改 URL + model 两个配置**，不涉及代码。

``pi.llm.embedding.EmbeddingClient``（pi 集成走的那条，经 adapters.py）同样只认
这一种形状，两边一致。

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


class EmbeddingError(Exception):
    """Raised on any embedding failure; the retriever logs-and-degrades."""


class HttpEmbedder:
    def __init__(
        self,
        url: str,
        api_key: str,
        model: str,
        timeout: float = TIMEOUT,
        batch_size: int = 16,
        retries: int = RETRIES,
        retry_backoff_s: float = RETRY_BACKOFF_S,
        transport: httpx.AsyncBaseTransport | None = None,  # test seam (mock transport)
    ) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.batch_size = max(1, batch_size)
        self.attempts = 1 + max(0, int(retries))
        self.retry_backoff_s = max(0.0, float(retry_backoff_s))
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
        body: dict = {"model": self.model, "input": texts}
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

        return self._parse(payload, len(texts))

    @staticmethod
    def _validate_vec(vec: object) -> None:
        if not isinstance(vec, list) or not vec:
            raise EmbeddingError("embedding entry is empty or not a list")
        if not all(isinstance(x, (int, float)) for x in vec):
            raise EmbeddingError("embedding entry is not a list of floats")

    def _parse(self, payload: dict, n: int) -> EmbedResult:
        try:
            entries = payload["data"]
        except (KeyError, TypeError) as exc:
            # OpenAI-compatible endpoints put auth/model errors under "error".
            err = payload.get("error") if isinstance(payload, dict) else None
            msg = err.get("message") if isinstance(err, dict) else str(payload)[:300]
            raise EmbeddingError(f"malformed embedding response: {msg}") from exc
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
                raise EmbeddingError(f"malformed embedding entry: {exc}") from exc
            self._validate_vec(vec)
            by_index[int(idx)] = list(vec)
        if len(by_index) != n:
            raise EmbeddingError("embedding response has duplicate/missing indices")
        usage = payload.get("usage") or {}
        tokens = usage.get("total_tokens") or usage.get("prompt_tokens") or 0
        return EmbedResult(vectors=[by_index[i] for i in range(n)], usage_tokens=int(tokens))
