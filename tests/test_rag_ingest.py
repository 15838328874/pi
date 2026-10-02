"""M2 ingest pipeline tests - orchestration correctness over the Protocols.

Deterministic layer: SqliteChunkStore (SQL truth) + FakeEmbedder + InMemory
vector store + MemoryBM25Index. This is the house test-layering rule (see
conftest): unit tests exercise pipeline LOGIC with in-process doubles; the
REAL corpus + REAL DashScope embedding path is verified by
tools/probe_m2_ingest.py and integration/ (PI_INTEGRATION=1), not mocked here.

Every async body runs inside asyncio.run(main()) - no pytest-asyncio.

What is pinned here (regressions that would silently corrupt retrieval):
- idempotency: re-ingest replaces chunks (delete-then-insert), doc row stays 1
- quality gate: image/scanned -> NEEDS_HEAVY_PARSER, zero chunks written
- graceful degrade: no embedder / embedder raises -> INDEX_PENDING, SQL intact,
  text still BM25-searchable, partial vectors cleaned up
- ACL: user A's chunks never reachable by user B (SQL WHERE + vector shard)
- rebuild_index: repairs INDEX_PENDING and repopulates a cleared vector store
- lexical invalidation: re-ingest changes chunk_ids, so a stale BM25 cache
  would hydrate wrong chunks - ingest MUST invalidate it
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pi.rag.config import ChunkingConfig, EmbeddingConfig, RagConfig
from pi.rag.defaults.bm25 import MemoryBM25Index
from pi.rag.defaults.fake_embedder import FakeEmbedder
from pi.rag.defaults.memory_vector import InMemoryVectorStore
from pi.rag.defaults.sqlite_store import SqliteChunkStore
from pi.rag.ingest import IngestPipeline
from pi.rag.types import EmbedResult, IngestStatus

# 1x1 PNG magic bytes - enough for sniff_kind to route to parse_image (which
# flags needs_heavy_parser without decoding). Real corpus PNGs covered in probe.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


# -- test doubles -----------------------------------------------------------


class FailingEmbedder:
    """Embedder whose embed() always raises - drives the INDEX_PENDING path."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> EmbedResult:
        self.calls += 1
        raise RuntimeError("embedding endpoint 503 (simulated outage)")

    async def embed_query(self, text: str) -> EmbedResult:
        return await self.embed([text])


class RecordingHooks:
    """UsageHooks that record calls - asserts metering fires (never raises)."""

    def __init__(self) -> None:
        self.embed_usage: list[tuple[int, int, str]] = []
        self.retrieval: list[tuple[str, float]] = []

    async def on_embed_usage(self, user_id: int, tokens: int, kind: str = "embedding") -> None:
        self.embed_usage.append((user_id, tokens, kind))

    async def on_retrieval(self, outcome: str, duration_s: float) -> None:
        self.retrieval.append((outcome, duration_s))


class BillingEmbedder(FakeEmbedder):
    """FakeEmbedder that reports non-zero usage so hooks can be asserted."""

    async def embed(self, texts: list[str]) -> EmbedResult:
        res = await super().embed(texts)
        return EmbedResult(vectors=res.vectors, usage_tokens=len(texts) * 7)


def _make_store(tmp_path: Path) -> SqliteChunkStore:
    return SqliteChunkStore(tmp_path / "rag.sqlite3")


def _make_pipeline(store, embedder, vector, *, hooks=None, lexical=None, cfg=None):
    return IngestPipeline(
        store=store,
        embedder=embedder,
        vector_store=vector,
        config=cfg or RagConfig(chunking=ChunkingConfig(max_chars=300, min_chars=20)),
        hooks=hooks,
        lexical_index=lexical,
    )


def _write_md(tmp_path: Path, name: str = "doc.md") -> Path:
    p = tmp_path / name
    p.write_text(
        "# 检索增强生成\n\n"
        "## 混合检索\n\n"
        "向量检索负责语义匹配，BM25 负责词法精确匹配，两者通过 RRF 融合排序。\n\n"
        "## 降级策略\n\n"
        "当向量库不可用时，系统自动降级到 BM25，再到 SQL LIKE，检索永不消失。\n",
        encoding="utf-8",
    )
    return p


# ---------------------------------------------------------------------------
# Happy path + idempotency
# ---------------------------------------------------------------------------


def test_ingest_happy_path_ready_and_searchable(tmp_path):
    store = _make_store(tmp_path)
    vec = InMemoryVectorStore()
    emb = FakeEmbedder()
    pipe = _make_pipeline(store, emb, vec)
    md = _write_md(tmp_path)

    async def main():
        out = await pipe.ingest_file(md, user_id=1, doc_key="rag-doc")
        assert out.status == IngestStatus.READY.value
        assert out.degraded is False
        assert out.chunks_stored >= 2
        assert out.chunks_indexed == out.chunks_stored  # every chunk vectorized

        # doc row recorded READY
        doc = await store.get_doc(1, "rag-doc")
        assert doc["status"] == IngestStatus.READY.value
        assert doc["title"] == "检索增强生成"  # parser-detected h1

        # chunks carry contextual title_path into embed_text
        chunks = await store.list_chunks_for_user(1)
        assert chunks
        hybrid = next(c for c in chunks if "混合检索" in c.title_path)
        assert hybrid.embed_text.startswith(hybrid.title_path)
        assert not hybrid.text.startswith(hybrid.title_path)  # cited text clean

        # vector index populated -> semantic search returns this user's chunk
        qv = (await emb.embed_query("RRF 融合 向量 BM25")).vectors[0]
        hits = await vec.search(1, qv, k=3)
        assert hits
        assert all(isinstance(cid, int) and cid > 0 for cid, _ in hits)

    asyncio.run(main())


def test_ingest_is_idempotent_delete_then_insert(tmp_path):
    """Re-ingesting the same doc_key must REPLACE chunks, not duplicate them.

    This is the delete-then-insert contract: chunk count is stable across runs,
    the rag_docs row stays singular, and chunk_ids are reassigned (which is why
    the lexical cache must be invalidated - see test_reingest_invalidates_lexical).
    """
    store = _make_store(tmp_path)
    vec = InMemoryVectorStore()
    pipe = _make_pipeline(store, FakeEmbedder(), vec)
    md = _write_md(tmp_path)

    async def main():
        first = await pipe.ingest_file(md, user_id=1, doc_key="rag-doc")
        ids_1 = sorted(c.chunk_id for c in await store.list_chunks_for_user(1))

        second = await pipe.ingest_file(md, user_id=1, doc_key="rag-doc")
        chunks_2 = await store.list_chunks_for_user(1)
        ids_2 = sorted(c.chunk_id for c in chunks_2)

        # count stable, not doubled
        assert second.chunks_stored == first.chunks_stored
        assert len(chunks_2) == first.chunks_stored
        # exactly one doc row for the key
        docs = await store.list_docs(1)
        assert len([d for d in docs if d["doc_key"] == "rag-doc"]) == 1
        # chunk_ids were reassigned (delete-then-insert), proving fresh rows
        assert ids_1 != ids_2
        # vector store also has no stale duplicates for this doc
        qv = (await FakeEmbedder().embed_query("混合检索 RRF")).vectors[0]
        hits = await vec.search(1, qv, k=50)
        assert len({cid for cid, _ in hits}) == len(hits)  # unique ids
        assert len(hits) == first.chunks_stored  # no leftover vectors

    asyncio.run(main())


def test_replace_chunks_replaces_atomically_and_returns_fresh_ids(tmp_path):
    """R2: store.replace_chunks does DELETE + INSERT in ONE transaction, so a
    concurrent reader never observes the doc with zero chunks (a window that
    exists in the old two-transaction delete_chunks -> add_chunks form and
    reads as "doc has no content" = silent miss).

    Verifies the observable contract: old chunks are gone, new chunks are in,
    the doc row is untouched, and the returned ids map back to the new rows.
    """
    from pi.rag.types import Chunk

    store = _make_store(tmp_path)
    pipe = _make_pipeline(store, FakeEmbedder(), InMemoryVectorStore())
    md = _write_md(tmp_path)

    async def main():
        await pipe.ingest_file(md, user_id=1, doc_key="rag-doc")
        before = await store.list_chunks_for_user(1)
        old_ids = {c.chunk_id for c in before}
        assert before

        # Replace with a cut-down set: 2 chunks instead of the full corpus.
        new_chunks = [
            Chunk(chunk_id=0, doc_key="rag-doc", user_id=1, seq=0,
                  text="只有一个块 A", embed_text="", title_path="块A", page=None),
            Chunk(chunk_id=0, doc_key="rag-doc", user_id=1, seq=1,
                  text="只有一个块 B", embed_text="", title_path="块B", page=None),
        ]
        ids = await store.replace_chunks(1, "rag-doc", new_chunks)

        after = await store.list_chunks_for_user(1)
        assert len(after) == 2
        assert {c.text for c in after} == {"只有一个块 A", "只有一个块 B"}
        # no old chunk survives
        assert old_ids.isdisjoint({c.chunk_id for c in after})
        # returned ids map 1:1 to the fresh rows (no collision)
        assert set(ids) == {c.chunk_id for c in after}
        assert len(set(ids)) == 2
        # doc row untouched
        assert (await store.get_doc(1, "rag-doc")) is not None

        # empty chunk set = atomic clear of the doc's chunks
        cleared = await store.replace_chunks(1, "rag-doc", [])
        assert cleared == []
        assert await store.list_chunks_for_user(1) == []

    asyncio.run(main())


def test_ingest_image_flagged_needs_heavy_parser(tmp_path):
    store = _make_store(tmp_path)
    pipe = _make_pipeline(store, FakeEmbedder(), InMemoryVectorStore())
    png = tmp_path / "公式.png"
    png.write_bytes(_PNG_BYTES)

    async def main():
        out = await pipe.ingest_file(png, user_id=1, doc_key="formula-img")
        assert out.status == IngestStatus.NEEDS_HEAVY_PARSER.value
        assert out.degraded is True
        assert out.chunks_stored == 0  # NOTHING indexed - no garbage in the store
        assert "OCR" in out.reason or "image" in out.reason.lower()
        doc = await store.get_doc(1, "formula-img")
        assert doc["status"] == IngestStatus.NEEDS_HEAVY_PARSER.value
        assert await store.list_chunks_for_user(1) == []

    asyncio.run(main())


def test_ingest_unsupported_kind_marks_failed(tmp_path):
    import zipfile

    store = _make_store(tmp_path)
    pipe = _make_pipeline(store, FakeEmbedder(), InMemoryVectorStore())
    z = tmp_path / "archive.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("random.bin", b"x")

    async def main():
        out = await pipe.ingest_file(z, user_id=1, doc_key="bad-zip")
        assert out.status == IngestStatus.FAILED.value
        assert out.degraded is True
        assert out.chunks_stored == 0
        doc = await store.get_doc(1, "bad-zip")
        assert doc["status"] == IngestStatus.FAILED.value
        assert doc["error"]  # reason persisted for audit

    asyncio.run(main())


def test_ingest_empty_content_marks_failed_not_ready(tmp_path):
    """A file that parses but yields zero chunks must NOT be silently READY."""
    store = _make_store(tmp_path)
    pipe = _make_pipeline(store, FakeEmbedder(), InMemoryVectorStore())
    blank = tmp_path / "blank.md"
    blank.write_text("   \n\n  \n", encoding="utf-8")  # whitespace only

    async def main():
        out = await pipe.ingest_file(blank, user_id=1, doc_key="blank")
        assert out.status == IngestStatus.FAILED.value
        assert out.chunks_stored == 0
        assert "no chunks" in out.reason

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Graceful degradation (never lose the document over a transient vector fault)
# ---------------------------------------------------------------------------


def test_ingest_no_embedder_index_pending_but_text_searchable(tmp_path):
    """Standalone without an embedding endpoint: text still lands in SQL and is
    BM25-searchable; status is INDEX_PENDING so rebuild-index can add vectors."""
    store = _make_store(tmp_path)
    lexical = MemoryBM25Index(store)
    pipe = _make_pipeline(store, None, None, lexical=lexical)
    md = _write_md(tmp_path)

    async def main():
        out = await pipe.ingest_file(md, user_id=1, doc_key="rag-doc")
        assert out.status == IngestStatus.INDEX_PENDING.value
        assert out.degraded is True
        assert out.chunks_stored >= 2
        assert out.chunks_indexed == 0
        # BM25 (lexical) still serves the content
        hits = await lexical.search(1, "向量 BM25 RRF 融合", k=5)
        assert hits
        cid = hits[0][0]
        got = await store.get_chunks_by_ids([cid])
        assert got and "RRF" in got[0].text

    asyncio.run(main())


def test_ingest_embedder_raises_index_pending_sql_intact(tmp_path):
    """Embedder outage mid-ingest: SQL truth survives, partial vectors cleaned,
    status INDEX_PENDING (not FAILED) - the doc is recoverable via rebuild."""
    store = _make_store(tmp_path)
    vec = InMemoryVectorStore()
    failing = FailingEmbedder()
    pipe = _make_pipeline(store, failing, vec)
    md = _write_md(tmp_path)

    async def main():
        out = await pipe.ingest_file(md, user_id=1, doc_key="rag-doc")
        assert out.status == IngestStatus.INDEX_PENDING.value
        assert out.degraded is True
        assert failing.calls >= 1
        assert out.chunks_stored >= 2  # text persisted despite embed failure
        assert out.chunks_indexed == 0
        assert "vector projection failed" in out.reason
        # SQL rows intact
        chunks = await store.list_chunks_for_user(1)
        assert len(chunks) == out.chunks_stored
        # no orphan vectors left behind for this doc
        qv = (await FakeEmbedder().embed_query("混合检索")).vectors[0]
        assert await vec.search(1, qv, k=10) == []

    asyncio.run(main())


# ---------------------------------------------------------------------------
# ACL isolation
# ---------------------------------------------------------------------------


def test_ingest_acl_user_isolation_sql_and_vector(tmp_path):
    """user 1's chunks must be invisible to user 2 through BOTH the SQL store
    (WHERE user_id) and the vector shard (per-user dict)."""
    store = _make_store(tmp_path)
    vec = InMemoryVectorStore()
    emb = FakeEmbedder()
    pipe = _make_pipeline(store, emb, vec)
    md1 = _write_md(tmp_path, "a.md")
    md2 = tmp_path / "b.md"
    md2.write_text(
        "# 财务报销\n\n## 流程\n\n报销需要提交发票和审批单，财务复核后打款。\n",
        encoding="utf-8",
    )

    async def main():
        await pipe.ingest_file(md1, user_id=1, doc_key="rag-doc")
        await pipe.ingest_file(md2, user_id=2, doc_key="fin-doc")

        # SQL: each user sees only their own chunks
        u1 = await store.list_chunks_for_user(1)
        u2 = await store.list_chunks_for_user(2)
        assert all("RRF" in c.text or "检索" in c.text for c in u1)
        assert all("报销" in c.text or "发票" in c.text for c in u2)
        assert not any(c.doc_key == "fin-doc" for c in u1)
        assert not any(c.doc_key == "rag-doc" for c in u2)

        # Vector: user 2's shard never returns user 1's chunk_ids
        u1_ids = {c.chunk_id for c in u1}
        qv = (await emb.embed_query("混合检索 RRF 向量")).vectors[0]
        hits_u2 = await vec.search(2, qv, k=20)
        assert hits_u2  # user 2 has vectors
        assert not (u1_ids & {cid for cid, _ in hits_u2})  # zero cross-user leak

    asyncio.run(main())


# ---------------------------------------------------------------------------
# rebuild_index + lexical invalidation
# ---------------------------------------------------------------------------


def test_rebuild_index_repairs_index_pending(tmp_path):
    """After an INDEX_PENDING ingest (no embedder), rebuilding with a real
    embedder populates the vector store and reports usage."""
    store = _make_store(tmp_path)
    md = _write_md(tmp_path)

    async def main():
        # phase 1: ingest with no embedder -> INDEX_PENDING, text in SQL
        p1 = _make_pipeline(store, None, None)
        out = await p1.ingest_file(md, user_id=1, doc_key="rag-doc")
        assert out.status == IngestStatus.INDEX_PENDING.value

        # phase 2: rebuild with an embedder + vector store -> vectors appear
        vec = InMemoryVectorStore()
        hooks = RecordingHooks()
        p2 = _make_pipeline(store, BillingEmbedder(), vec, hooks=hooks)
        summary = await p2.rebuild_index(1)
        assert summary["status"] == "ok"
        assert summary["indexed"] == summary["total"] == out.chunks_stored
        assert summary["usage_tokens"] > 0
        # usage was metered through the hook
        assert hooks.embed_usage and hooks.embed_usage[0][1] == summary["usage_tokens"]
        # vectors now searchable
        qv = (await FakeEmbedder().embed_query("RRF 融合")).vectors[0]
        assert await vec.search(1, qv, k=3)

    asyncio.run(main())


def test_rebuild_index_repopulates_cleared_vector_store(tmp_path):
    """InMemory vectors are lost on restart; rebuild_index restores them from
    the SQL source of truth without re-parsing (proves SQL is the truth)."""
    store = _make_store(tmp_path)
    vec = InMemoryVectorStore()
    emb = FakeEmbedder()
    pipe = _make_pipeline(store, emb, vec)
    md = _write_md(tmp_path)

    async def main():
        out = await pipe.ingest_file(md, user_id=1, doc_key="rag-doc")
        assert out.status == IngestStatus.READY.value
        qv = (await emb.embed_query("降级 BM25")).vectors[0]
        assert await vec.search(1, qv, k=3)

        # simulate process restart: vector store emptied, SQL untouched
        await vec.close()
        fresh = InMemoryVectorStore()
        pipe2 = _make_pipeline(store, emb, fresh)
        assert await fresh.search(1, qv, k=3) == []  # gone

        summary = await pipe2.rebuild_index(1)
        assert summary["indexed"] == out.chunks_stored
        assert await fresh.search(1, qv, k=3)  # restored from SQL

    asyncio.run(main())


def test_reingest_invalidates_lexical_cache(tmp_path):
    """Regression: re-ingest reassigns chunk_ids. A cached BM25 shard built on
    the OLD ids would return ids that hydrate to wrong/missing chunks. Ingest
    must invalidate the lexical cache so the next search rebuilds from SQL."""
    store = _make_store(tmp_path)
    lexical = MemoryBM25Index(store)
    pipe = _make_pipeline(store, FakeEmbedder(), InMemoryVectorStore(), lexical=lexical)

    async def main():
        # v1 content
        md = tmp_path / "doc.md"
        md.write_text("# 版本一\n\n苹果香蕉橙子水果清单。\n", encoding="utf-8")
        await pipe.ingest_file(md, user_id=1, doc_key="fruit")
        hits_v1 = await lexical.search(1, "苹果 香蕉", k=3)
        assert hits_v1
        old_ids = {cid for cid, _ in hits_v1}

        # warm the cache, then re-ingest DIFFERENT content under same doc_key
        md.write_text("# 版本二\n\n汽车火车飞机交通工具清单。\n", encoding="utf-8")
        await pipe.ingest_file(md, user_id=1, doc_key="fruit")

        # old terms must no longer match (cache was invalidated, rebuilt from SQL)
        hits_old = await lexical.search(1, "苹果 香蕉", k=3)
        assert hits_old == [] or not (old_ids & {cid for cid, _ in hits_old})
        # new terms match, and hydrate to the CURRENT chunk rows
        hits_new = await lexical.search(1, "汽车 火车", k=3)
        assert hits_new
        got = await store.get_chunks_by_ids([hits_new[0][0]])
        assert got and "汽车" in got[0].text  # correct hydration, not a stale id

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Usage metering
# ---------------------------------------------------------------------------


def test_ingest_reports_embed_usage_to_hooks(tmp_path):
    store = _make_store(tmp_path)
    hooks = RecordingHooks()
    pipe = _make_pipeline(store, BillingEmbedder(), InMemoryVectorStore(), hooks=hooks)
    md = _write_md(tmp_path)

    async def main():
        out = await pipe.ingest_file(md, user_id=7, doc_key="rag-doc")
        assert out.status == IngestStatus.READY.value
        assert out.usage_tokens == out.chunks_stored * 7
        assert (7, out.usage_tokens, "embedding") in hooks.embed_usage

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Vector projection batching + metering on partial failure
# ---------------------------------------------------------------------------


class RecordingVectorStore(InMemoryVectorStore):
    """Records the size of each upsert call."""

    def __init__(self) -> None:
        super().__init__()
        self.upsert_batches: list[int] = []

    async def upsert(self, chunks, vectors) -> None:
        self.upsert_batches.append(len(chunks))
        await super().upsert(chunks, vectors)


class HalfwayFailingVectorStore(InMemoryVectorStore):
    """Accepts ``fail_after`` upserts, then fails - a mid-document hiccup."""

    def __init__(self, fail_after: int = 1) -> None:
        super().__init__()
        self.fail_after = fail_after
        self.calls = 0

    async def upsert(self, chunks, vectors) -> None:
        self.calls += 1
        if self.calls > self.fail_after:
            raise RuntimeError("milvus unavailable")
        await super().upsert(chunks, vectors)


def _batched_cfg(batch_size: int) -> RagConfig:
    cfg = RagConfig(chunking=ChunkingConfig(max_chars=300, min_chars=20))
    cfg.embedding = EmbeddingConfig(batch_size=batch_size)
    return cfg


def _write_long_md(tmp_path: Path, paragraphs: int = 8) -> Path:
    """A document long enough to need SEVERAL embed/upsert batches."""
    p = tmp_path / "long.md"
    body = "\n\n".join(
        f"## 第{i}节 血糖管理要点\n"
        + "糖尿病患者的血糖控制需要结合饮食、运动与药物三方面综合管理。" * 6
        for i in range(paragraphs)
    )
    p.write_text(f"# 慢病管理指南\n\n{body}\n", encoding="utf-8")
    return p


def test_vector_upsert_is_batched_like_the_embedding(tmp_path):
    """One giant upsert per document would build a ~20MB single Milvus request
    for a 5000-chunk doc and hold every vector in memory. Batch count and total
    must track the embedding batch size."""
    async def main():
        store = _make_store(tmp_path)
        vec = RecordingVectorStore()
        pipe = _make_pipeline(store, BillingEmbedder(), vec, cfg=_batched_cfg(2))
        out = await pipe.ingest_file(_write_long_md(tmp_path), user_id=7, doc_key="rag-doc")

        assert out.status == IngestStatus.READY.value
        assert out.chunks_stored >= 3, "test needs at least two batches"
        assert sum(vec.upsert_batches) == out.chunks_stored, "every chunk upserted once"
        assert max(vec.upsert_batches) <= 2, f"batches not capped: {vec.upsert_batches}"
        assert len(vec.upsert_batches) == -(-out.chunks_stored // 2), "one call per batch"

    asyncio.run(main())


def test_partial_projection_failure_still_bills_tokens_already_spent(tmp_path):
    """Embedding IS billable even when the projection fails later. Reporting 0
    usage for a partially-embedded doc silently under-bills the user - and the
    quota callback is the only place that spend is ever counted.

    Batches are embed-then-upsert, so when upsert #2 blows up the vectors for
    that batch were ALREADY paid for at the provider (embed() returned them
    successfully); only the storage write failed. Honest accounting must bill
    both batches = 4 texts x 7 tokens = 28, not just the first one."""
    async def main():
        hooks = RecordingHooks()
        vec = HalfwayFailingVectorStore(fail_after=1)
        pipe = _make_pipeline(store=_make_store(tmp_path), embedder=BillingEmbedder(),
                              vector=vec, hooks=hooks, cfg=_batched_cfg(2))
        out = await pipe.ingest_file(_write_long_md(tmp_path), user_id=7, doc_key="rag-doc")

        assert out.chunks_stored >= 3, "test needs a second batch to fail on"
        assert out.status == IngestStatus.INDEX_PENDING.value
        assert out.chunks_indexed == 0, "partial vectors are purged, so nothing is indexed"
        assert out.usage_tokens == 28, "2 batches of 2 texts x 7 tokens (embed ran before upsert failed)"
        assert (7, 28, "embedding") in hooks.embed_usage
        assert vec.calls == 2, "must fail fast - do not keep embedding after a write fails"

    asyncio.run(main())


