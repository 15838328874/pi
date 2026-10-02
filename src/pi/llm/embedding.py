"""Embedding client for any OpenAI-compatible ``/embeddings`` endpoint (httpx only).

Config: PI_EMBEDDING_URL / PI_EMBEDDING_API_KEY / PI_EMBEDDING_MODEL.
Request:  POST {url}  Authorization: Bearer {key}
          {"model": ..., "input": ["...", ...]}
Response: {"data": [{"index": i, "embedding": [floats]}, ...],
           "usage": {"prompt_tokens": N, "total_tokens": N}}

**Deliberately OpenAI-shaped, not provider-native.** 早先本模块只认 DashScope 原生
契约（请求 ``{"input":{"texts":[...]}}``、应答 ``{"output":{"embeddings":[...]}}``），
那等于把 embedding 绑死在一家厂商和一种端点形态上——换模型就得改代码，且原生端点
与 OpenAI 兼容端点混用时，配错**不会报错**，只会静默产生漂移向量（检索质量下降
但看不出原因）。

OpenAI 的 ``/embeddings`` 是事实标准：阿里云 MaaS 的 ``/compatible-mode/v1``、
OpenAI 官方、DeepSeek、vLLM、Ollama、TEI 等都提供同一形状。所以这里只实现它，
换模型只需要改 ``PI_EMBEDDING_URL`` + ``PI_EMBEDDING_MODEL`` 两个环境变量。

Callers (MemoryRepo) treat any EmbeddingError as "log and degrade": text rows
remain lexically searchable, so embedding failures must never fail a run. The
token usage is returned so the caller can meter the spend (embedding tokens
are billed like LLM tokens).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

TIMEOUT = 30.0  # module const, mirroring pi.tools.web


class EmbeddingError(Exception):
    """Raised on any embedding failure; callers log-and-degrade."""


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    usage_tokens: int = 0


class EmbeddingClient:
    def __init__(self, url: str, api_key: str, model: str, timeout: float = TIMEOUT) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        """Embed a batch of texts; vectors are in input order.

        Raises EmbeddingError on transport errors, non-2xx, or malformed
        responses (count mismatch, non-float vectors, bad index).
        """
        try:
            # trust_env=False: the dev/prod host can carry dead http(s)_proxy env
            # vars; they must not leak into outbound calls (they hang or refuse).
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                resp = await client.post(
                    self.url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    # OpenAI shape: input is a bare string or an array of strings.
                    json={"model": self.model, "input": texts},
                )
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embedding request failed: {exc}") from exc
        if resp.status_code != 200:
            raise EmbeddingError(f"embedding endpoint returned HTTP {resp.status_code}")

        try:
            payload = resp.json()
            entries = payload["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingError(f"malformed embedding response: {exc}") from exc
        if len(entries) != len(texts):
            raise EmbeddingError(
                f"embedding count mismatch: asked {len(texts)}, got {len(entries)}"
            )

        # Defensive: order by the response's own index instead of trusting order.
        by_index: dict[int, list[float]] = {}
        for entry in entries:
            try:
                vec = entry["embedding"]
                idx = entry.get("index", len(by_index))
            except (KeyError, TypeError) as exc:
                raise EmbeddingError(f"malformed embedding entry: {exc}") from exc
            if not isinstance(vec, list) or not all(
                isinstance(x, (int, float)) for x in vec
            ):
                raise EmbeddingError("embedding entry is not a list of floats")
            if not vec:
                raise EmbeddingError("embedding entry is empty")
            by_index[idx] = vec
        usage = payload.get("usage", {}).get("total_tokens", 0)
        return EmbeddingResult(
            vectors=[by_index[i] for i in range(len(texts))],
            usage_tokens=int(usage or 0),
        )
