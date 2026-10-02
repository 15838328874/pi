"""Real-stack RAG integration: MySQL truth + Milvus projection + REAL embedding.

This is the test the user asked for: NOT SQLite/InMemory/Fake - the actual
production-shaped stack end to end on local infra:
  local MySQL (pi_py_test)  = SQL source of truth (rag_docs/rag_chunks)
  local Milvus (pi_rag_chunks_itest) = vector projection, PK=chunk_id
  real DashScope embedding (qwen3.7-text-embedding, dim=1024, real billing)

Flow exercised: real corpus file -> IngestPipeline.ingest_file -> parse -> chunk
-> REAL embed -> MySQL rows + Milvus vectors -> vector search closes the loop ->
rebuild_index repopulates Milvus from MySQL after a simulated flush -> ACL
isolation holds through BOTH MySQL and Milvus -> re-ingest idempotency (no ghost
vectors). Every assertion checks the REAL backend, not a double.

Enable with PI_INTEGRATION=1 + PI_ITEST_* (deliberately not PI_* names:
tests/conftest.py pins those to ""). Point them at LOCAL infra, e.g.:

    PI_INTEGRATION=1 \
    PI_ITEST_DATABASE_URL=mysql+aiomysql://pi:pi_py_local@127.0.0.1:3306/pi_py_test \
    PI_ITEST_MILVUS_URI=http://127.0.0.1:19531 \
    PI_ITEST_EMBEDDING_URL=... PI_ITEST_EMBEDDING_API_KEY=... PI_ITEST_EMBEDDING_MODEL=... \
    pytest integration/test_rag_real.py -q -s

Cleanup: drops the throwaway user's MySQL rows and the itest Milvus collection.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from integration.conftest import require_embedding_vars
from pi.rag.config import ChunkingConfig, EmbeddingConfig, RagConfig
from pi.rag.defaults.bm25 import MemoryBM25Index
from pi.rag.defaults.http_embedder import HttpEmbedder
from pi.rag.defaults.milvus_vector import MilvusRagVectorStore
from pi.rag.defaults.mysql_store import MysqlChunkStore
from pi.rag.ingest import IngestPipeline
from pi.rag.types import IngestStatus

pytestmark = pytest.mark.skipif(
    os.environ.get("PI_INTEGRATION") != "1",
    reason="real-stack integration; set PI_INTEGRATION=1 + PI_ITEST_* to run",
)

_ITEST_COLLECTION = "pi_rag_chunks_itest"


def _corpus() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "待测试文档"
        if cand.is_dir():
            return cand
    return None


def _require_corpus() -> Path:
    c = _corpus()
    if c is None:
        pytest.skip("待测试文档 corpus not found")
    return c


def test_rag_real_stack_chain():
    require_embedding_vars()
    if not os.environ.get("PI_ITEST_DATABASE_URL"):
        pytest.skip("missing PI_ITEST_DATABASE_URL")
    corpus = _require_corpus()

    db_url = os.environ["PI_ITEST_DATABASE_URL"]
    milvus_uri = os.environ["PI_ITEST_MILVUS_URI"]
    emb_cfg = EmbeddingConfig(
        url=os.environ["PI_ITEST_EMBEDDING_URL"],
        api_key=os.environ["PI_ITEST_EMBEDDING_API_KEY"],
        model=os.environ["PI_ITEST_EMBEDDING_MODEL"],
        
    )
    stamp = int(time.time())
    uid = 990000 + (stamp % 90000)  # throwaway user id (no FK on rag_* tables)

    async def main() -> None:
        store = MysqlChunkStore(db_url, create_schema=True)
        vec = MilvusRagVectorStore(uri=milvus_uri, collection=_ITEST_COLLECTION)
        lexical = MemoryBM25Index(store)
        emb = HttpEmbedder(
            url=emb_cfg.url, api_key=emb_cfg.api_key, model=emb_cfg.model
        )
        cfg = RagConfig(chunking=ChunkingConfig(max_chars=600, min_chars=60), embedding=emb_cfg)
        pipe = IngestPipeline(
            store=store, embedder=emb, vector_store=vec, config=cfg, lexical_index=lexical
        )

        assert await vec.ping(), "local Milvus not reachable"
        # Clean slate on BOTH sides. The itest collection is dropped wholesale
        # (legal: it is a disposable projection of rag_chunks, not the truth),
        # so a previous aborted run can never leak stale vectors into this one.
        await vec.drop()
        await store.delete_doc(uid, "rag-eval")

        try:
            # --- 1) REAL ingest: corpus -> MySQL + Milvus via real embedder ----
            md = corpus / "RAG评估.md"
            assert md.is_file(), f"corpus file missing: {md}"
            out = await pipe.ingest_file(md, user_id=uid, doc_key="rag-eval")
            print(f"\n[ingest] status={out.status} stored={out.chunks_stored} "
                  f"indexed={out.chunks_indexed} usage={out.usage_tokens}")
            assert out.status == IngestStatus.READY.value, out.reason
            assert out.chunks_stored >= 3
            assert out.chunks_indexed == out.chunks_stored
            assert out.usage_tokens > 0, "real embedder must bill tokens"

            # --- 2) MySQL really holds the rows (source of truth) -------------
            rows = await store.list_chunks_for_user(uid)
            assert len(rows) == out.chunks_stored, (len(rows), out.chunks_stored)
            doc = await store.get_doc(uid, "rag-eval")
            assert doc and doc["status"] == IngestStatus.READY.value
            print(f"[mysql] rag_docs.status={doc['status']} rag_chunks={len(rows)}")

            # --- 3) Milvus really holds the vectors; semantic search works ---
            q = "RAG 检索质量怎么评估，有哪些指标"
            qv = (await emb.embed_query(q)).vectors[0]
            assert len(qv) == 1024, f"expected dim 1024, got {len(qv)}"
            hits = await vec.search(uid, qv, k=5)
            print(f"[milvus] vector hits for query: {hits[:3]}")
            assert hits, "Milvus returned no hits for an ingested doc"
            assert len(hits) == out.chunks_stored or len(hits) == 5
            # hydrate the top hit back through MySQL (1:1 PK contract)
            top = await store.get_chunks_by_ids([hits[0][0]])
            assert top and top[0].user_id == uid
            assert "评估" in top[0].title_path or "评估" in top[0].text
            print(f"[hydrate] top chunk title_path={top[0].title_path!r}")

            # --- 4) BM25 lexical channel also serves (hybrid-ready) ----------
            lex = await lexical.search(uid, "评估 指标 RAGAS", k=3)
            assert lex, "BM25 returned nothing on real MySQL rows"

            # --- 5) ACL: another user gets ZERO hits (MySQL + Milvus) --------
            qv2 = qv
            other_hits = await vec.search(uid + 1, qv2, k=10)
            assert other_hits == [], f"cross-user Milvus leak: {other_hits}"
            assert await store.list_chunks_for_user(uid + 1) == []
            print("[acl] other-user Milvus hits = 0, MySQL rows = 0")

            # --- 6) rebuild_index: flush Milvus, repopulate from MySQL -------
            await vec.delete_by_doc(uid, "rag-eval")
            assert await vec.search(uid, qv, k=3) == [], "vectors should be gone"
            summary = await pipe.rebuild_index(uid)
            print(f"[rebuild] status={summary['status']} indexed={summary['indexed']}/{summary['total']} "
                  f"usage={summary['usage_tokens']}")
            assert summary["status"] == "ok"
            assert summary["indexed"] == out.chunks_stored
            assert await vec.search(uid, qv, k=3), "rebuild did not repopulate Milvus"

            # --- 7) re-ingest idempotency: no ghost vectors ------------------
            before = len(await vec.search(uid, qv, k=100))
            out2 = await pipe.ingest_file(md, user_id=uid, doc_key="rag-eval")
            assert out2.status == IngestStatus.READY.value
            after_rows = await store.list_chunks_for_user(uid)
            after_vec = len(await vec.search(uid, qv, k=100))
            print(f"[idempotent] chunks {out.chunks_stored}->{len(after_rows)} "
                  f"vectors {before}->{after_vec}")
            assert len(after_rows) == out.chunks_stored, "MySQL chunks duplicated on re-ingest"
            assert after_vec == len(after_rows), "ghost vectors after re-ingest"
        finally:
            # Zero residue: drop the throwaway user's MySQL rows AND the itest
            # Milvus collection (delete_by_doc alone leaves the empty collection
            # behind, which is the residue this test must not create).
            await store.delete_doc(uid, "rag-eval")
            await vec.drop()
            await vec.close()
            await store.dispose()

    asyncio.run(main())
