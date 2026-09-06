"""Embedding providers for memory retrieval.

Reuses the OpenAI-compatible endpoint the chat providers already use
(OPENAI_API_KEY / OPENAI_BASE_URL), so a domestic model gateway that serves chat
also serves embeddings with no new dependency and no new credential.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from collections.abc import Sequence
from typing import Protocol

from pi.models import Usage

log = logging.getLogger("pi.memory.embed")


class Embedder(Protocol):
    async def embed(self, texts: Sequence[str]) -> tuple[list[list[float]], Usage]:
        """Vectors in input order, plus token usage for metering."""


class OpenAIEmbedder:
    """Embeddings via any OpenAI-compatible endpoint.

    `dim` is sent as the `dimensions` request parameter (MRL truncation, supported by
    text-embedding-3-* and text-embedding-v3). Set it to 0 to use the model's native
    dimension instead - MemoryService then probes the real width before creating the
    collection, so the two can never disagree.
    """

    def __init__(
        self,
        model: str,
        dim: int = 0,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
    ):
        from openai import AsyncOpenAI

        self.model = model
        self.dim = int(dim)
        self._client = AsyncOpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY"),
            base_url=base_url or os.environ.get("OPENAI_BASE_URL"),
            timeout=timeout,
        )

    async def embed(self, texts: Sequence[str]) -> tuple[list[list[float]], Usage]:
        if not texts:
            return [], Usage()
        kwargs: dict[str, object] = {"model": self.model, "input": list(texts)}
        if self.dim > 0:
            kwargs["dimensions"] = self.dim
        resp = await self._client.embeddings.create(**kwargs)  # type: ignore[arg-type]
        # The API may return items out of order; `index` is the input position.
        ordered = sorted(resp.data, key=lambda d: d.index)
        vectors = [list(d.embedding) for d in ordered]
        prompt_tokens = getattr(getattr(resp, "usage", None), "prompt_tokens", 0) or 0
        return vectors, Usage(input_tokens=int(prompt_tokens), output_tokens=0)


_WORD = re.compile(r"\w+", re.UNICODE)


class HashEmbedder:
    """Deterministic bag-of-words hash embedding for tests and keyless dev.

    Token overlap drives cosine similarity, so dedup, retrieval and eviction tests
    exercise real behaviour with no network, no API key and no cost. hashlib rather
    than the builtin hash(): the latter is per-process randomised, which would make
    test expectations unreproducible.
    """

    def __init__(self, dim: int = 64):
        self.dim = int(dim)
        self.calls = 0
        self.texts_embedded = 0

    async def embed(self, texts: Sequence[str]) -> tuple[list[list[float]], Usage]:
        self.calls += 1
        vectors: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in _WORD.findall(text.lower()):
                bucket = int(hashlib.sha256(tok.encode("utf-8")).hexdigest(), 16) % self.dim
                vec[bucket] += 1.0
            norm = math.sqrt(sum(x * x for x in vec))
            vectors.append([x / norm for x in vec] if norm else vec)
        self.texts_embedded += len(texts)
        chars = sum(len(t) for t in texts)
        return vectors, Usage(input_tokens=chars // 4, output_tokens=0)


def get_embedder(model: str, dim: int = 0) -> Embedder | None:
    """None when no embedding model is configured - memory cannot work without one.

    A retrieval store with no way to vectorise the query is useless, so this is a
    hard off-switch rather than a silent fallback to fake vectors in production.
    """
    if not model:
        return None
    if model.startswith("hash/"):
        return HashEmbedder(dim=int(model.partition("/")[2] or 64))
    return OpenAIEmbedder(model, dim=dim)
