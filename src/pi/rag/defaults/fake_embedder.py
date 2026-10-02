"""Deterministic embedding stub - tests and offline standalone mode only.

NOT a real embedder: it hashes bag-of-terms into a fixed-dim vector, so
identical/similar term sets get similar vectors (enough for RRF/degradation
unit tests and offline smoke runs), but semantic quality is meaningless.
Production uses HttpEmbedder (DashScope contract) or the pi EmbeddingClient
via adapters.py.
"""

from __future__ import annotations

import hashlib
import math
import re

from pi.rag.types import EmbedResult

_TERM_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]")


def _terms(text: str) -> list[str]:
    # Same tokenizer spirit as MemoryRepo._terms in pi.server.db: ascii words
    # + single CJK chars. Bigrams of CJK chars give a bit of phrase signal.
    out: list[str] = []
    toks = _TERM_RE.findall(text.lower())
    out.extend(toks)
    for i in range(len(toks) - 1):
        a, b = toks[i], toks[i + 1]
        if _TERM_RE.fullmatch(a) and len(a) == 1 and ord(a) > 0x4E00:
            out.append(a + b)
    return out


class FakeEmbedder:
    """Hash-of-terms embedder. dim is fixed so vectors are comparable."""

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    async def embed(self, texts: list[str]) -> EmbedResult:
        vectors = [self._embed_one(t) for t in texts]
        # usage_tokens=0: the fake path must not bill quota in tests.
        return EmbedResult(vectors=vectors, usage_tokens=0)

    async def embed_query(self, text: str) -> EmbedResult:
        return await self.embed([text])

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for term in _terms(text):
            h = hashlib.md5(term.encode("utf-8")).digest()
            idx = int.from_bytes(h[:4], "big") % self.dim
            sign = 1.0 if h[4] % 2 == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]
