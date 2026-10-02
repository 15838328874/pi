"""The five seams that keep the RAG kernel portable.

Every external capability enters the kernel through one of these Protocols.
Defaults live in ``pi.rag.defaults`` (usable standalone); the pi integration
re-implements them in ``pi.rag.adapters`` (the ONLY file importing pi.*).

Design rules baked into these signatures:
- Everything is async; blocking libraries (pymilvus, sqlite) must be wrapped
  in ``asyncio.to_thread`` by the implementer, never by the caller.
- Failure semantics: methods may raise. The RETRIEVER owns the degradation
  chain (vector fail -> BM25, BM25 fail -> SQL LIKE) - stores never swallow
  errors silently, and the retriever reports every degradation via
  ``UsageHooks.on_retrieval``.
- ACL: every read/write carries ``user_id`` as an int. Implementations MUST
  filter by it server-side (Milvus: bare int literal in the filter string;
  SQL: WHERE user_id=?). The retriever treats user_id as untrusted input
  coming from the caller (ToolContext.user_db_id).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from pi.rag.types import Chunk, DocMeta, EmbedResult, RetrievedChunk


@runtime_checkable
class Embedder(Protocol):
    """Text -> vectors. Mirrors pi.llm.embedding.EmbeddingClient's contract."""

    async def embed(self, texts: list[str]) -> EmbedResult:
        """Embed a batch; vectors in input order. Raise EmbeddingError-like
        exceptions on any failure - callers log-and-degrade."""

    async def embed_query(self, text: str) -> EmbedResult:
        """Embed a single query. Defaults to embed([text]); asymmetric
        embedding models (query vs passage prefixes) can override."""


@runtime_checkable
class ChunkStore(Protocol):
    """SQL source of truth for docs + chunks (rag_docs / rag_chunks).

    The vector index is a rebuildable projection of this store - never the
    other way around. ``list_chunks_for_user`` feeds both rebuild-index and
    the in-memory BM25 index.
    """

    async def upsert_doc(self, doc: DocMeta, status: str, error: str = "") -> int:
        """Insert or update doc metadata by (user_id, doc_key); return the SQL id."""

    async def get_doc(self, user_id: int, doc_key: str) -> dict | None:
        """Doc row as dict, or None. ACL: scoped to user_id."""

    async def list_docs(self, user_id: int) -> list[dict]:
        """All docs for one user (ACL-scoped)."""

    async def delete_doc(self, user_id: int, doc_key: str) -> int:
        """Delete doc + its chunks (cascade). Returns chunks deleted."""

    async def add_chunks(self, chunks: list[Chunk]) -> list[int]:
        """Persist chunks WITHOUT chunk_id set; returns assigned ids in input
        order. Idempotency is the ingest pipeline's job (delete-then-insert
        per doc_key), not this method's."""

    async def delete_chunks(self, user_id: int, doc_key: str) -> int:
        """Delete ONLY the chunks of one doc, keeping the rag_docs row intact.

        This is the idempotency primitive for re-ingest: ingest calls
        upsert_doc(PENDING) -> delete_chunks -> add_chunks, so the doc row (and
        its created_at / status history) survives a re-parse while stale chunks
        are cleared. Distinct from delete_doc, which cascades the doc away.
        Returns the number of chunks deleted. ACL: WHERE user_id=? AND doc_key=?
        """

    async def replace_chunks(
        self, user_id: int, doc_key: str, chunks: list[Chunk]
    ) -> list[int]:
        """Atomically replace one doc's chunks: DELETE existing + INSERT new in
        a SINGLE transaction. Returns assigned ids in input order (same mapping
        guarantees as add_chunks).

        Re-ingest via ``delete_chunks`` + ``add_chunks`` (two transactions)
        leaves a window where a concurrent reader sees the doc with ZERO chunks
        - and "no chunks" is indistinguishable from "doc has no content", so a
        reader can silently miss a doc that is simply being re-ingested. One
        transaction closes that window. ACL: WHERE user_id=? AND doc_key=? on
        the delete; the insert rows carry the same doc_key/user_id.
        """

    async def list_chunks_for_user(self, user_id: int) -> list[Chunk]:
        """All chunks for one user, ordered by (doc_key, seq). Feeds BM25
        index build and rebuild-index. ACL: WHERE user_id=?"""

    async def get_chunks_by_ids(self, chunk_ids: Sequence[int]) -> list[Chunk]:
        """Hydrate vector hits back to full chunks. ACL is enforced by the
        retriever passing only ids already filtered by user_id; implementations
        SHOULD still filter defensively."""

    async def search_text(self, user_id: int, query: str, k: int) -> list[RetrievedChunk]:
        """Last-resort SQL LIKE fallback (vector AND lexical both down).
        Returns degraded hits - never raises for 'not found'."""


@runtime_checkable
class RagVectorStore(Protocol):
    """Vector index over chunks (default backend: Milvus collection
    ``pi_rag_chunks``; also an InMemory implementation for tests/standalone).

    Inherits the MilvusStore house pattern: lazy import + lazy construct,
    asyncio.to_thread for blocking calls, proxy env disabled, int filters as
    bare literals. PK = chunk_id (the SQL row id) so hits hydrate 1:1.
    """

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        """Idempotent upsert keyed by chunk_id. len(vectors) == len(chunks)."""

    async def delete_by_doc(self, user_id: int, doc_key: str) -> None:
        """Drop all vectors of one doc (re-ingest path)."""

    async def search(
        self, user_id: int, vector: list[float], k: int, doc_keys: list[str] | None = None
    ) -> list[tuple[int, float]]:
        """Top-k (chunk_id, score) for ONE user, best first. doc_keys narrows
        to specific docs (optional filter). Raise on backend failure - the
        retriever degrades to BM25."""

    async def ping(self) -> bool:
        """Health probe; never raises (False on any failure)."""

    async def drop(self) -> None:
        """Drop the WHOLE projection (ops/test primitive, never user-facing).

        Legal only because of the truth-source rule: SQL (rag_chunks) owns the
        bytes, this is a rebuildable projection, so ``IngestPipeline.
        rebuild_index`` re-derives everything. Use for schema migrations
        (embedding dim change) and for zero-residue integration cleanup.
        Never raises.
        """

    async def close(self) -> None:
        """Release resources; safe to call twice."""


@runtime_checkable
class LexicalIndex(Protocol):
    """BM25-style lexical retrieval (default: in-memory inverted index).

    Per-user sharded; built lazily from ChunkStore on first use and
    invalidated on ingest. Tokenization for Chinese: char bigrams +
    word terms (jieba when available), matching the repo's _TERM_RE spirit.
    """

    async def search(self, user_id: int, query: str, k: int) -> list[tuple[int, float]]:
        """Top-k (chunk_id, bm25_score). Raise when the index cannot be
        built (no store / store down) - the retriever then falls to SQL."""

    async def invalidate(self, user_id: int) -> None:
        """Drop the cached index for one user (called after ingest)."""


@runtime_checkable
class Reranker(Protocol):
    """Cross-encoder precision stage. Optional: None -> skip rerank.

    Failure semantics: raise on transport/endpoint errors; the retriever
    skips rerank, keeps RRF order, and reports outcome=rerank_failed.
    """

    async def rerank(self, query: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """Return the same chunks re-scored and re-ordered (may drop tails).
        usage reporting goes through UsageHooks, wired by the retriever."""


@runtime_checkable
class UsageHooks(Protocol):
    """Metering + observability seam (pi: MemoryRepo.on_embed_usage /
    on_retrieval equivalents; standalone: no-op default).

    Hook implementations MUST never raise into the retrieval path - the
    retriever wraps every call in try/except and logs.
    """

    async def on_embed_usage(self, user_id: int, tokens: int, kind: str = "embedding") -> None:
        """kind: 'embedding' | 'rerank' - both bill against the user quota."""

    async def on_retrieval(self, outcome: str, duration_s: float) -> None:
        """outcome enum (see types.RetrievalMode + failure outcomes):
        hybrid_ok / vector_only / bm25_fallback / sql_fallback /
        embed_failed / rerank_failed / error"""


@runtime_checkable
class HeavyParser(Protocol):
    """External OCR service for scans/images (v1.5, now landed).

    Blocking by design (OCR jobs are second-to-minute scale and polled); the
    caller runs it via asyncio.to_thread. Returns Markdown text, raises
    HeavyParserError on any failure. Vendor-swappable: PaddleOCR today,
    MinerU behind the same protocol when a service exists.
    """

    def parse(self, path: Path) -> str:
        """File (image/scan) -> Markdown text. May raise; never returns ''."""
