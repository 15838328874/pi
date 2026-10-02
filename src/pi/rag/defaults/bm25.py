"""In-memory BM25 lexical index, per-user sharded.

Zero-infra hybrid retrieval: this is both the BM25 channel of normal hybrid
search AND the first degradation step when embedding/Milvus fail (house
philosophy: 词法兜底，检索降级不消失).

Tokenizer: ascii words + CJK single chars + CJK bigrams (jieba when importable,
bigram fallback otherwise - deterministic, no model downloads, works offline).

Index lifecycle: built lazily from ChunkStore.list_chunks_for_user on first
query for that user, cached in memory, invalidated after ingest. NOT durable -
that's fine, SQL is the truth and rebuild is cheap.

STALENESS ACROSS PROCESSES (R1): ``invalidate()`` can only clear the shard in
the process that calls it. With several server workers behind one load
balancer, a ``rag ingest`` served by worker A leaves workers B..N answering
from their pre-ingest shard until they restart - silently wrong answers, while
the VECTOR channel (Milvus = shared state) already sees the new chunks. Pass
``ttl_s`` to bound that window; production wiring does
(``RetrievalConfig.bm25_ttl_s``, default 300s, ``PI_RAG_BM25_TTL``). The
constructor default is 0 = TTL off, so standalone single-process users, the CLI
and the A/B harness keep the exact behaviour they always had.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections import Counter

from pi.rag.protocols import ChunkStore
from pi.rag.types import Chunk

_TERM_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]")

# BM25 constants (Robertson/Okapi defaults). k1 dampens term frequency,
# b normalizes for document length. Kept as module constants with intent.
_K1 = 1.5
_B = 0.75

# How many times _ensure() will retry a build that raced an invalidate().
# Each retry is one SQL read; 3 attempts means a pathological ingest-during-
# query storm degrades to "answer from a fresh uncached build", never to a
# stale shard and never to an unbounded loop.
_BUILD_ATTEMPTS = 3


def tokenize(text: str) -> list[str]:
    """ascii words + CJK unigrams + CJK bigrams. Deterministic, offline."""
    toks = _TERM_RE.findall(text.lower())
    out = list(toks)
    for i in range(len(toks) - 1):
        a, b = toks[i], toks[i + 1]
        if len(a) == 1 and "\u4e00" <= a <= "\u9fff" and len(b) == 1 and "\u4e00" <= b <= "\u9fff":
            out.append(a + b)
    return out


class _UserIndex:
    """BM25 structures for one user's shard."""

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunk_ids: list[int] = []
        self.doc_key: dict[int, str] = {}
        self.tf: list[Counter] = []
        self.df: Counter = Counter()
        self.avgdl = 0.0
        total_len = 0
        for ch in chunks:
            # Index the heading path too, not just the body - see
            # Chunk.text_to_index(). Without it, terms that live ONLY in a
            # heading ("权限", "contextual", clause numbers) get zero lexical
            # hits while the vector channel (which embeds title_path) finds
            # them, making hybrid worse than vector-only for exact-term lookups.
            toks = tokenize(ch.text_to_index())
            if not toks:
                continue
            c = Counter(toks)
            self.chunk_ids.append(ch.chunk_id)
            self.doc_key[ch.chunk_id] = ch.doc_key
            self.tf.append(c)
            for t in set(toks):
                self.df[t] += 1
            total_len += len(toks)
        n = len(self.tf) or 1
        self.avgdl = total_len / n

    def score(self, query_toks: list[str], k: int) -> list[tuple[int, float]]:
        if not self.tf:
            return []
        n = len(self.tf)
        qterms = set(query_toks)
        scores: list[tuple[int, float]] = []
        # Precompute IDF per query term (BM25+ style, floored at 0 so rare
        # terms never contribute negatively).
        idf = {t: max(0.0, math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))) for t in qterms}
        for i, cid in enumerate(self.chunk_ids):
            tf_i = self.tf[i]
            dl = sum(tf_i.values()) or 1
            s = 0.0
            for t in qterms:
                f = tf_i.get(t, 0)
                if f:
                    s += idf[t] * (f * (_K1 + 1)) / (f + _K1 * (1 - _B + _B * dl / self.avgdl))
            if s > 0:
                scores.append((cid, s))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:k]


class MemoryBM25Index:
    """LexicalIndex implementation backed by a ChunkStore.

    Args:
        store: the SQL truth source every shard is built from.
        ttl_s: max age of a cached per-user shard before it is rebuilt from
            SQL. 0 (default) = no TTL, the shard lives until invalidated or the
            process exits. Production sets this from
            ``RetrievalConfig.bm25_ttl_s``; see the module docstring for the
            multi-worker staleness bug it bounds.

    ``ttl_s`` is a keyword-only-style optional so that every existing
    single-argument call site (CLI, A/B harness, 17 test constructions) keeps
    its exact previous behaviour and the shipped eval baselines stay
    reproducible.
    """

    def __init__(self, store: ChunkStore, ttl_s: float = 0.0) -> None:
        self._store = store
        self._ttl_s = float(ttl_s)
        self._by_user: dict[int, _UserIndex] = {}
        # When each shard was built (monotonic - wall clocks jump backwards on
        # NTP correction, which would make a shard look permanently fresh).
        self._built_at: dict[int, float] = {}
        # Bumped by invalidate(). A build that STARTED under epoch N and lands
        # under epoch N+1 was reading SQL that an ingest has since overtaken;
        # storing it would cache a shard nobody will ever invalidate again.
        self._epoch = 0
        self._locks: dict[int, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    def _build_index(self, chunks: list[Chunk]) -> _UserIndex:
        """Construction seam: which VIEW of these chunks becomes the index.

        Exists so the M4 A/B harness can measure alternatives (prose-only
        corpus, field-weighted titles) against the shipped default without
        either re-implementing the index or mutating production behaviour.
        Overriding this is the supported extension point; overriding
        ``_ensure`` is not (it owns the per-user lock + double-check).
        """
        return _UserIndex(chunks)

    async def _lock_for(self, user_id: int) -> asyncio.Lock:
        async with self._guard:
            if user_id not in self._locks:
                self._locks[user_id] = asyncio.Lock()
            return self._locks[user_id]

    def _fresh(self, user_id: int) -> bool:
        """Is the cached shard usable, or must it be rebuilt from SQL?"""
        if user_id not in self._by_user:
            return False
        if self._ttl_s <= 0:
            return True  # TTL disabled: valid until invalidate()
        age = time.monotonic() - self._built_at.get(user_id, 0.0)
        return age < self._ttl_s

    async def _ensure(self, user_id: int) -> _UserIndex:
        if self._fresh(user_id):
            return self._by_user[user_id]
        lock = await self._lock_for(user_id)
        async with lock:
            for attempt in range(_BUILD_ATTEMPTS):
                if self._fresh(user_id):  # double-check after await
                    return self._by_user[user_id]
                epoch = self._epoch
                chunks = await self._store.list_chunks_for_user(user_id)
                index = self._build_index(chunks)
                if self._epoch == epoch:
                    now = time.monotonic()
                    self._by_user[user_id] = index
                    self._built_at[user_id] = now
                    return index
                # An ingest invalidated while we were reading. Our snapshot
                # predates it; publishing would cache pre-ingest data under a
                # fresh timestamp (i.e. restart the TTL clock on stale data).
                # Re-read instead - bounded, so a query storm can't spin.
            # Last resort: rebuild once more and publish regardless. Answering
            # from a just-read (possibly one-ingest-behind) shard beats either
            # looping or returning a shard we know is stale, and the TTL will
            # correct it within bm25_ttl_s.
            chunks = await self._store.list_chunks_for_user(user_id)
            index = self._build_index(chunks)
            self._by_user[user_id] = index
            self._built_at[user_id] = time.monotonic()
            return index

    async def search(self, user_id: int, query: str, k: int) -> list[tuple[int, float]]:
        idx = await self._ensure(int(user_id))
        toks = tokenize(query)
        if not toks:
            return []
        return idx.score(toks, max(1, int(k)))

    async def invalidate(self, user_id: int) -> None:
        # Bump the epoch FIRST: a build already in flight must see the new
        # epoch when it lands, so it discards its pre-invalidate snapshot.
        self._epoch += 1
        uid = int(user_id)
        self._by_user.pop(uid, None)
        self._built_at.pop(uid, None)
