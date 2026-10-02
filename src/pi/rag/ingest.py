"""Ingest pipeline (M2): file -> parsed -> chunked -> embedded -> stored.

This module is STORE-AGNOSTIC pure orchestration over the Protocols - the same
code path serves standalone (SqliteChunkStore + InMemoryVectorStore) and the pi
integration (MySQL ChunkStore + Milvus RagVectorStore, injected via adapters).
It never imports pi.*, never touches a concrete backend, and never blocks the
event loop (blocking work lives behind the store/embedder Protocol impls).

House rules enforced here:
- SQL is the SOURCE OF TRUTH; the vector index is a rebuildable projection. So
  text lands in SQL FIRST. If embedding or vector upsert fails, the doc is
  marked INDEX_PENDING (still BM25-searchable, rebuild-index repairs it) rather
  than FAILED - we never lose the user's document over a transient vector hiccup.
- Idempotency = delete-then-insert per doc_key (upsert_doc PENDING -> delete_chunks
  -> add_chunks). Re-ingesting the same doc_key replaces its chunks cleanly; the
  rag_docs row (created_at/history) survives.
- Quality gate: a parsed doc flagged needs_heavy_parser is recorded as
  NEEDS_HEAVY_PARSER and NOT indexed - ingesting scanned garbage would poison
  retrieval silently (the whole point of the M1 quality gate).
- Never silent: every non-READY terminal status carries a reason, and embed
  usage is reported through UsageHooks (best-effort, never raises into ingest).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from pi.rag.chunker import Chunker
from pi.rag.config import RagConfig
from pi.rag.parser import parse_file
from pi.rag.protocols import ChunkStore, Embedder, LexicalIndex, RagVectorStore, UsageHooks
from pi.rag.types import (
    Chunk,
    ChunkDraft,
    DocMeta,
    IngestOutcome,
    IngestStatus,
    ParseResult,
)

log = logging.getLogger("pi.rag.ingest")

__all__ = ["IngestPipeline", "IngestOutcome"]


class IngestPipeline:
    """Orchestrates parse -> chunk -> embed -> persist -> project.

    Constructed with the Protocols (dependency injection). ``vector_store`` and
    ``embedder`` may be None: standalone deployments without an embedding
    endpoint still ingest (text -> SQL -> BM25), landing INDEX_PENDING so
    rebuild-index can add vectors later once an embedder is configured.
    """

    def __init__(
        self,
        store: ChunkStore,
        embedder: Embedder | None,
        vector_store: RagVectorStore | None,
        *,
        config: RagConfig | None = None,
        chunker: Chunker | None = None,
        hooks: UsageHooks | None = None,
        lexical_index: LexicalIndex | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.vector_store = vector_store
        self.config = config or RagConfig()
        self.chunker = chunker or Chunker(self.config.chunking)
        self.hooks = hooks
        # Optional: when a BM25 index is shared with the retriever, ingest must
        # invalidate the user's cached shard after a re-ingest (chunk_ids change
        # under delete-then-insert, so a stale cache would hydrate wrong chunks).
        self.lexical_index = lexical_index

    # -- public API ---------------------------------------------------------

    async def ingest_file(
        self,
        path: str | Path,
        *,
        user_id: int,
        doc_key: str | None = None,
        title: str | None = None,
        visibility: str = "private",
        source: str | None = None,
    ) -> IngestOutcome:
        """Ingest one file end to end. Idempotent on (user_id, doc_key).

        doc_key defaults to the absolute path string (stable across re-runs for
        the same file). title defaults to the parser's detected title. ``source``
        is the display/citation origin (rag_docs.source_path); defaults to the
        path. Callers that ingest from a spooled temp copy (e.g. the HTTP upload
        endpoint) should pass the ORIGINAL filename here, so citations don't leak
        a dead temp path.
        """
        p = Path(path)
        key = doc_key or str(p.resolve())
        outcome = IngestOutcome(doc_key=key)

        # 1) PARSE (blocking -> to_thread). Parse failure is terminal FAILED.
        try:
            parsed: ParseResult = await asyncio.to_thread(
                parse_file,
                p,
                max_pdf_pages=self.config.max_pdf_pages,
                min_density=self.config.min_text_density,
            )
        except Exception as exc:  # noqa: BLE001 - ingest must not crash the caller
            reason = f"{type(exc).__name__}: {exc}"
            log.warning("ingest parse failed for %s: %s", key, reason)
            await self._record_status(key, user_id, p, title or "", visibility,
                                      IngestStatus.FAILED.value, reason)
            outcome.status = IngestStatus.FAILED.value
            outcome.reason = reason[:300]
            outcome.degraded = True
            return outcome

        outcome.title = title or parsed.title or p.stem

        # 2) QUALITY GATE: scanned/complex layout -> record, do NOT index.
        if parsed.needs_heavy_parser:
            reason = parsed.reason or "low text density / garbled (likely scanned)"
            await self._record_status(key, user_id, p, outcome.title, visibility,
                                      IngestStatus.NEEDS_HEAVY_PARSER.value, reason)
            outcome.status = IngestStatus.NEEDS_HEAVY_PARSER.value
            outcome.reason = reason[:300]
            outcome.degraded = True
            return outcome

        # 3) CHUNK (pure). Empty parse (no blocks) -> FAILED, not a silent READY.
        drafts: list[ChunkDraft] = self.chunker.chunk(parsed)
        if not drafts:
            reason = "parser produced no chunks (empty or unsupported content)"
            await self._record_status(key, user_id, p, outcome.title, visibility,
                                      IngestStatus.FAILED.value, reason)
            outcome.status = IngestStatus.FAILED.value
            outcome.reason = reason
            outcome.degraded = True
            return outcome

        # 4) SQL SOURCE OF TRUTH first: PENDING -> delete old chunks -> insert.
        doc = DocMeta(
            doc_key=key,
            user_id=int(user_id),
            title=outcome.title,
            source_path=source or str(p),
            visibility=visibility,
        )
        await self.store.upsert_doc(doc, status=IngestStatus.PENDING.value)
        # Purge the OLD projection on BOTH sides before re-inserting. The vector
        # store must drop this doc's stale vectors too, or re-ingest (which
        # reassigns chunk_ids) leaves GHOST vectors pointing at ids that no
        # longer hydrate -> phantom/wrong hits at query time.
        if self.vector_store is not None:
            try:
                await self.vector_store.delete_by_doc(int(user_id), key)
            except Exception:  # noqa: BLE001 - stale-vector purge is best-effort;
                # rebuild_index reconciles, and a failed purge must not fail ingest
                log.warning("could not purge stale vectors for %s", key, exc_info=True)
        chunks = [self._to_chunk(d, key, int(user_id)) for d in drafts]
        # replace_chunks = delete + insert in ONE transaction: re-ingest's old
        # two-transaction form (delete_chunks -> add_chunks) left a window where
        # a concurrent reader saw the doc with zero chunks and silently missed it.
        ids = await self.store.replace_chunks(int(user_id), key, chunks)
        for ch, cid in zip(chunks, ids):
            ch.chunk_id = int(cid)
        outcome.chunks_stored = len(chunks)

        # 5) VECTOR PROJECTION (best-effort): embed -> upsert. Any failure here
        #    leaves SQL intact and marks INDEX_PENDING (BM25 still serves).
        indexed, usage, reason = await self._project_vectors(chunks, int(user_id), key)
        outcome.chunks_indexed = indexed
        outcome.usage_tokens = usage
        if reason:
            outcome.reason = reason
            outcome.degraded = True

        # 6) terminal status
        if indexed == len(chunks) and not reason:
            final = IngestStatus.READY.value
        else:
            final = IngestStatus.INDEX_PENDING.value
            outcome.degraded = True
        await self.store.upsert_doc(doc, status=final, error=outcome.reason)
        outcome.status = final

        # 7) lexical index for this user is now stale -> drop cached BM25
        await self._invalidate_lexical(int(user_id))

        return outcome

    async def rebuild_index(self, user_id: int) -> dict:
        """Re-embed everything for one user and re-project the vector index.

        Reads the SQL source of truth (list_chunks_for_user), re-embeds in
        batches, upserts vectors. Used to (a) repair INDEX_PENDING docs after a
        transient embedder outage, (b) repopulate InMemoryVectorStore after a
        restart, (c) migrate Milvus collections. Returns a small summary dict.
        """
        chunks = await self.store.list_chunks_for_user(int(user_id))
        summary = {"user_id": int(user_id), "total": len(chunks), "indexed": 0,
                   "usage_tokens": 0, "status": "ok", "reason": ""}
        if not chunks:
            return summary
        if self.embedder is None or self.vector_store is None:
            summary["status"] = "index_pending"
            summary["reason"] = "no embedder/vector_store configured"
            return summary

        usage = 0
        indexed = 0
        try:
            for batch in _batches(chunks, self.config.embedding.batch_size):
                texts = [c.text_to_embed() for c in batch]
                res = await self.embedder.embed(texts)
                usage += res.usage_tokens
                await self.vector_store.upsert(batch, res.vectors)
                indexed += len(batch)
        except Exception as exc:  # noqa: BLE001
            summary["status"] = "index_pending"
            summary["reason"] = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("rebuild_index partial for user %s: %s", user_id, summary["reason"])

        summary["indexed"] = indexed
        summary["usage_tokens"] = usage
        await self._report_usage(int(user_id), usage, "embedding")
        await self._invalidate_lexical(int(user_id))
        return summary

    # -- internals ----------------------------------------------------------

    def _to_chunk(self, d: ChunkDraft, doc_key: str, user_id: int) -> Chunk:
        return Chunk(
            chunk_id=0,  # assigned by store.add_chunks
            doc_key=doc_key,
            user_id=user_id,
            seq=d.seq,
            text=d.text,
            embed_text=d.embed_text,
            title_path=d.title_path,
            page=d.page,
        )

    async def _project_vectors(
        self, chunks: list[Chunk], user_id: int, doc_key: str
    ) -> tuple[int, int, str]:
        """Embed + upsert one doc's chunks. Returns (indexed_count, usage, reason).

        reason is "" on full success. Any embedder/vector failure is caught and
        reported as a reason (-> INDEX_PENDING); the SQL rows are already safe.
        """
        if self.embedder is None or self.vector_store is None:
            return 0, 0, "no embedder/vector_store configured (text stored, BM25-searchable)"
        usage = 0
        indexed = 0
        try:
            texts = [c.text_to_embed() for c in chunks]
            # Embed AND upsert in the same batch shape. Upserting the whole doc in
            # one call would build a ~20MB single Milvus request for a 5000-chunk
            # document and hold every vector in memory at once - which fails or
            # thrashes exactly on the big documents this corpus is full of.
            for part_texts, part_chunks in _paired_batches(
                texts, chunks, self.config.embedding.batch_size
            ):
                res = await self.embedder.embed(part_texts)
                if len(res.vectors) != len(part_chunks):
                    raise ValueError(
                        f"embedder returned {len(res.vectors)} vectors for "
                        f"{len(part_chunks)} chunks"
                    )
                usage += res.usage_tokens
                await self.vector_store.upsert(part_chunks, list(res.vectors))
                indexed += len(part_chunks)
            await self._report_usage(user_id, usage, "embedding")
            return len(chunks), usage, ""
        except Exception as exc:  # noqa: BLE001
            reason = f"vector projection failed: {type(exc).__name__}: {exc}"
            log.warning("ingest %s -> INDEX_PENDING: %s", doc_key, reason)
            # best-effort cleanup: drop any partially-upserted vectors for this doc
            try:
                await self.vector_store.delete_by_doc(user_id, doc_key)
            except Exception:  # noqa: BLE001 - cleanup is best-effort
                pass
            # Report the tokens that WERE spent before the failure: embedding
            # happened (and is billable) even though the projection did not
            # land. Returning 0 here would silently under-bill the user.
            if usage:
                await self._report_usage(user_id, usage, "embedding")
            return 0, usage, reason[:300]

    async def _record_status(
        self, doc_key: str, user_id: int, path: Path, title: str,
        visibility: str, status: str, reason: str
    ) -> None:
        """Persist a terminal status for a doc that never reached READY."""
        doc = DocMeta(
            doc_key=doc_key, user_id=int(user_id), title=title or path.stem,
            source_path=str(path), visibility=visibility,
        )
        try:
            await self.store.upsert_doc(doc, status=status, error=reason[:500])
        except Exception:  # noqa: BLE001 - status bookkeeping must not mask the real error
            log.warning("could not record status %s for %s", status, doc_key)

    async def _report_usage(self, user_id: int, tokens: int, kind: str) -> None:
        if not self.hooks or tokens <= 0:
            return
        try:
            await self.hooks.on_embed_usage(user_id, tokens, kind)
        except Exception:  # noqa: BLE001 - hooks never raise into the pipeline
            log.debug("on_embed_usage hook failed", exc_info=True)

    async def _invalidate_lexical(self, user_id: int) -> None:
        """Drop the cached BM25 shard for this user after its chunks changed.

        Correctness, not a nicety: re-ingest does delete-then-insert, so chunk
        ids are reassigned. A stale lexical cache would return old ids that now
        hydrate to different (or deleted) chunks. When no lexical index is wired
        (e.g. ingest run standalone, retriever builds its own lazily from SQL),
        this is a safe no-op.
        """
        if self.lexical_index is None:
            return
        try:
            await self.lexical_index.invalidate(int(user_id))
        except Exception:  # noqa: BLE001 - a failed invalidate must not fail ingest
            log.warning("lexical invalidate failed for user %s", user_id, exc_info=True)


def _batches(items: list, size: int):
    size = max(1, size)
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _paired_batches(texts: list[str], chunks: list[Chunk], size: int):
    """Yield (text_slice, chunk_slice) pairs so vectors can be checked 1:1."""
    size = max(1, size)
    for i in range(0, len(texts), size):
        yield texts[i : i + size], chunks[i : i + size]
