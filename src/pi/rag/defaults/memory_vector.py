"""In-memory vector store - tests + offline standalone fallback.

Cosine similarity, brute-force over per-user shards. NOT for production
scale (O(n) per query, lost on restart); its job is:
  1. unit/integration tests with zero infra,
  2. standalone mode where Milvus isn't configured - retrieval still works
     (degrades gracefully, and rebuild-index repopulates from SQL).

ACL contract: search() filters by user_id at the shard level - a query for
user A never even iterates user B's vectors. Mirrors the Milvus int-filter
guarantee with an in-process equivalent.
"""

from __future__ import annotations

import math
from collections import defaultdict

from pi.rag.types import Chunk


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class InMemoryVectorStore:
    def __init__(self) -> None:
        # user_id -> {chunk_id: (vector, doc_key)}
        self._shards: dict[int, dict[int, tuple[list[float], str]]] = defaultdict(dict)
        self._closed = False

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError(f"upsert mismatch: {len(chunks)} chunks vs {len(vectors)} vectors")
        for ch, vec in zip(chunks, vectors):
            self._shards[int(ch.user_id)][int(ch.chunk_id)] = (list(vec), ch.doc_key)

    async def delete_by_doc(self, user_id: int, doc_key: str) -> None:
        shard = self._shards.get(int(user_id), {})
        for cid in [cid for cid, (_, dk) in shard.items() if dk == doc_key]:
            shard.pop(cid, None)

    async def search(
        self, user_id: int, vector: list[float], k: int, doc_keys: list[str] | None = None
    ) -> list[tuple[int, float]]:
        # ACL: only this user's shard is scanned.
        shard = self._shards.get(int(user_id), {})
        allow = set(doc_keys) if doc_keys else None
        scored: list[tuple[int, float]] = []
        for cid, (vec, dk) in shard.items():
            if allow is not None and dk not in allow:
                continue
            scored.append((cid, _cosine(vector, vec)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[: max(1, int(k))]

    async def drop(self) -> None:
        """Drop every shard (ops/test primitive; mirrors MilvusRagVectorStore.drop).

        Legal under the truth-source rule: vectors are a disposable projection
        of rag_chunks. Never raises.
        """
        self._shards.clear()

    async def ping(self) -> bool:
        return not self._closed

    async def close(self) -> None:
        self._shards.clear()
        self._closed = True
