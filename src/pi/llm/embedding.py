"""Embedding client for a DashScope-style MaaS embedding endpoint (httpx only).

Config: PI_EMBEDDING_URL / PI_EMBEDDING_API_KEY / PI_EMBEDDING_MODEL.
Request:  POST {url}  Authorization: Bearer {key}
          {"model": ..., "input": {"texts": ["...", ...]}}
Response: {"output": {"embeddings": [{"text_index": i, "embedding": [floats]}]},
           "usage": {"total_tokens": N}}

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
        responses (count mismatch, non-float vectors, missing text_index).
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
                    json={"model": self.model, "input": {"texts": texts}},
                )
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embedding request failed: {exc}") from exc
        if resp.status_code != 200:
            raise EmbeddingError(f"embedding endpoint returned HTTP {resp.status_code}")

        try:
            entries = resp.json()["output"]["embeddings"]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingError(f"malformed embedding response: {exc}") from exc
        if len(entries) != len(texts):
            raise EmbeddingError(
                f"embedding count mismatch: asked {len(texts)}, got {len(entries)}"
            )

        # Defensive: order by text_index instead of trusting response order.
        by_index: dict[int, list[float]] = {}
        for entry in entries:
            try:
                vec = entry["embedding"]
                idx = entry.get("text_index", len(by_index))
            except (KeyError, TypeError) as exc:
                raise EmbeddingError(f"malformed embedding entry: {exc}") from exc
            if not isinstance(vec, list) or not all(
                isinstance(x, (int, float)) for x in vec
            ):
                raise EmbeddingError("embedding entry is not a list of floats")
            if not vec:
                raise EmbeddingError("embedding entry is empty")
            by_index[idx] = vec
        usage = resp.json().get("usage", {}).get("total_tokens", 0)
        return EmbeddingResult(
            vectors=[by_index[i] for i in range(len(texts))],
            usage_tokens=int(usage or 0),
        )
