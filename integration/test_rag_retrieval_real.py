"""Real-stack RETRIEVAL integration: M3 measured on real infra + real models.

test_rag_real.py proves the WRITE path (ingest -> MySQL -> Milvus). This file
proves the READ path under the user's standing requirement - 真模型 + 本地
Milvus + 本地 MySQL, no doubles:

  local MySQL (pi_py_test)        = SQL truth (rag_docs/rag_chunks)
  local Milvus (pi_rag_chunks_itest) = vector projection, PK=chunk_id
  real DashScope embedding         = qwen3.7-text-embedding (dim 1024)
  real DashScope rerank            = qwen3.7-text-rerank (skipped if unconfigured)

What it asserts, in order:
  1. hybrid retrieval returns HYBRID/hybrid_ok with citations hydrated from SQL
  2. EvalRunner + a golden set built from the REAL chunks -> Recall@k / MRR /
     hit@k are computed on real scores (先建评测再调检索 - the rig runs on truth)
  3. A/B on the real stack: vector_only vs hybrid vs hybrid+rerank, printed as
     the markdown table 对接文档 §6 step 4 asks for
  4. rerank really re-scores (real cross-encoder, billed via on_embed_usage)
  5. ACL: another user gets ZERO hits through the REAL retriever
  6. degradation on real infra: Milvus down -> bm25_fallback still serves,
     embedding down -> embed_failed, both down -> sql_fallback; every step
     reports its outcome (绝不静默)

The golden set is DERIVED FROM THE INGESTED CHUNKS (not hardcoded seq numbers),
so it survives a chunker change: gold = chunks whose text/title actually
contains the answer terms. That keeps the eval honest when切块 moves.

Enable with tools/run_rag_real.py (loads .env -> PI_ITEST_*), or set the
PI_ITEST_* vars manually. Zero residue: the itest collection is dropped.
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
from pi.rag.defaults.http_reranker import HttpReranker
from pi.rag.defaults.milvus_vector import MilvusRagVectorStore
from pi.rag.defaults.mysql_store import MysqlChunkStore
from pi.rag.eval.harness import GoldenQA, GoldenSet, ab_markdown, chunk_key
from pi.rag.eval.runner import EvalRunner
from pi.rag.ingest import IngestPipeline
from pi.rag.retriever import (
    OUTCOME_BM25_FALLBACK,
    OUTCOME_EMBED_FAILED,
    OUTCOME_HYBRID_OK,
    OUTCOME_SQL_FALLBACK,
    HybridRetriever,
)
from pi.rag.types import EmbedResult, IngestStatus, RetrievalMode

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


# -- doubles used ONLY to simulate outages on the real stack -----------------
# (the real backends are injected everywhere else; these exist so the
# degradation chain can be exercised without actually stopping MySQL/Milvus)


class _DownVectorStore:
    """Stands in for an unreachable Milvus."""

    async def search(self, user_id, vector, k, doc_keys=None):
        raise ConnectionError("milvus proxy unreachable (simulated)")

    async def ping(self) -> bool:
        return False


class _DownEmbedder:
    """Stands in for a dead embedding endpoint."""

    async def embed(self, texts):
        raise RuntimeError("embedding endpoint 503 (simulated)")

    async def embed_query(self, text):
        return await self.embed([text])


class _DownLexical:
    async def search(self, user_id, query, k):
        raise RuntimeError("bm25 build failed (simulated)")

    async def invalidate(self, user_id) -> None:
        return None


class _RecordingHooks:
    def __init__(self) -> None:
        self.usage: list[tuple[int, int, str]] = []
        self.outcomes: list[str] = []

    async def on_embed_usage(self, user_id, tokens, kind="embedding") -> None:
        self.usage.append((int(user_id), int(tokens), kind))

    async def on_retrieval(self, outcome, duration_s) -> None:
        self.outcomes.append(outcome)


def test_rag_real_retrieval():
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
    rerank_url = os.environ.get("PI_ITEST_RERANK_URL", "")
    rerank_key = os.environ.get("PI_ITEST_RERANK_API_KEY", "")
    rerank_model = os.environ.get("PI_ITEST_RERANK_MODEL", "")
    stamp = int(time.time())
    uid = 991000 + (stamp % 80000)  # throwaway uid, distinct range from test_rag_real

    async def main() -> None:
        store = MysqlChunkStore(db_url, create_schema=True)
        vec = MilvusRagVectorStore(uri=milvus_uri, collection=_ITEST_COLLECTION)
        lexical = MemoryBM25Index(store)
        emb = HttpEmbedder(
            url=emb_cfg.url, api_key=emb_cfg.api_key, model=emb_cfg.model
        )
        cfg = RagConfig(
            chunking=ChunkingConfig(max_chars=600, min_chars=60),
            embedding=emb_cfg,
        )
        cfg.retrieval.final_k = 5
        cfg.retrieval.vector_k = 10
        cfg.retrieval.bm25_k = 10

        assert await vec.ping(), "local Milvus not reachable"
        await vec.drop()  # clean slate (disposable projection)
        await store.delete_doc(uid, "rag-eval")

        try:
            # --- ingest the REAL corpus with the REAL embedder --------------
            md = corpus / "RAG评估.md"
            assert md.is_file(), f"corpus file missing: {md}"
            pipe = IngestPipeline(
                store=store, embedder=emb, vector_store=vec, config=cfg, lexical_index=lexical
            )
            out = await pipe.ingest_file(md, user_id=uid, doc_key="rag-eval")
            print(f"\n[ingest] status={out.status} stored={out.chunks_stored} "
                  f"indexed={out.chunks_indexed} usage={out.usage_tokens}")
            assert out.status == IngestStatus.READY.value, out.reason
            assert out.chunks_indexed == out.chunks_stored >= 5

            chunks = await store.list_chunks_for_user(uid)
            by_key = {chunk_key(c.doc_key, c.seq): c for c in chunks}
            print(f"[corpus] {len(chunks)} chunks in MySQL; sample title_paths:")
            for c in chunks[:4]:
                print(f"    {chunk_key(c.doc_key, c.seq)} :: {c.title_path!r}")

            # --- golden set DERIVED from the real chunks ---------------------
            def gold_for(*terms: str) -> list[str]:
                """chunk_keys whose text/title_path contains ANY of the terms."""
                return [
                    k for k, c in by_key.items()
                    if any(t in c.text or t in c.title_path
                           for t in terms)
                ]

            cases = []
            specs = [
                ("q1", "评估检索质量用哪些指标", ("Recall", "MRR", "context_recall",
                                                "context_relevancy", "上下文召回")),
                ("q2", "RAGAS 是什么评估框架", ("RAGAs", "RAGAS")),
                ("q3", "数据集格式包含哪些字段", ("ground_truths", "contexts", "question")),
                ("q4", "评估生成质量看什么", ("faithfulness", "忠实", "answer_relevancy")),
                ("q5", "人工评估的缺点", ("人工评估", "时间和人力")),
            ]
            for cid, query, terms in specs:
                keys = gold_for(*terms)
                assert keys, f"golden case {cid}: no chunk matched {terms} - corpus changed?"
                cases.append(GoldenQA(id=cid, query=query, user_id=uid,
                                      gold_chunk_keys=keys, category="happy_path"))
            # Note: an adversarial case (no gold in this corpus) is deliberately
            # NOT asserted here - Recall@k on a zero-gold case is undefined by
            # harness design. Adversarial coverage belongs in the M4 golden set
            # once a multi-doc corpus is ingested.
            golden = GoldenSet(name="real-rag-eval", cases=cases)
            print(f"[golden] {len(cases)} cases, gold sizes="
                  f"{[len(c.gold_chunk_keys) for c in cases]}")

            # --- the retriever under test: ALL real backends ----------------
            hooks = _RecordingHooks()
            retr = HybridRetriever(
                store, embedder=emb, vector_store=vec, lexical_index=lexical,
                reranker=None, config=cfg, hooks=hooks,
            )

            # 1) hybrid happy path with citations
            res = await retr.search(uid, "RAG 检索质量用哪些指标评估")
            print(f"[hybrid] mode={res.mode.value} outcome={res.outcome} "
                  f"degraded={res.degraded} hits={len(res.chunks)} {res.duration_ms}ms")
            assert res.mode is RetrievalMode.HYBRID, res
            assert res.outcome == OUTCOME_HYBRID_OK
            assert res.degraded is False
            assert res.chunks, "real hybrid retrieval returned nothing"
            top = res.chunks[0]
            assert top.title and top.source and top.title_path, (
                f"citations missing: title={top.title!r} source={top.source!r}"
            )
            print(f"[cite] top score={top.score:.4f} title={top.title!r} "
                  f"path={top.title_path!r}")
            print(f"[top1] {top.text[:120]!r}")

            # 2) EvalRunner over the REAL retriever -> real Recall/MRR
            runner = EvalRunner(store, k_max=5)
            rep_hybrid = await runner.run(
                golden, retr.search_chunks, "hybrid (vector+bm25+RRF)"
            )
            print("\n" + rep_hybrid.markdown())
            assert rep_hybrid.failed == 0, "a case errored on the real stack"
            # The corpus is small and the queries are near-verbatim, so a real
            # embedding model must nail these. Anything below this means the
            # real pipeline is broken, not that the model is weak.
            assert rep_hybrid.metrics["recall@5"] >= 0.8, rep_hybrid.markdown()
            assert rep_hybrid.metrics["mrr"] >= 0.5, rep_hybrid.markdown()
            assert rep_hybrid.metrics["hit@1"] >= 0.6, rep_hybrid.markdown()

            # 3) A/B on the real stack: vector_only vs hybrid vs hybrid+rerank
            vec_only_cfg = RagConfig(chunking=cfg.chunking, embedding=emb_cfg)
            vec_only_cfg.retrieval.final_k = 5
            vec_only_cfg.retrieval.vector_k = 10
            vec_only = HybridRetriever(
                store, embedder=emb, vector_store=vec, lexical_index=None,
                config=vec_only_cfg, hooks=_RecordingHooks(),
            )
            rep_vec = await runner.run(golden, vec_only.search_chunks, "vector_only")
            assert rep_vec.metrics, "vector_only produced no metrics"

            reports = [rep_vec, rep_hybrid]
            reranker = None
            rep_rerank = None
            retr_rerank = None
            if rerank_url and rerank_key:
                reranker = HttpReranker(url=rerank_url, api_key=rerank_key, model=rerank_model)
                retr_rerank = HybridRetriever(
                    store, embedder=emb, vector_store=vec, lexical_index=lexical,
                    reranker=reranker, config=cfg, hooks=hooks,
                )
                rep_rerank = await runner.run(
                    golden, retr_rerank.search_chunks, "hybrid+rerank"
                )
                reports.append(rep_rerank)
            else:
                print("\n[rerank] PI_ITEST_RERANK_* not set - A/B runs without the "
                      "cross-encoder stage")
            print("\n=== A/B on the REAL stack ===")
            print(ab_markdown(reports))

            # 4) real rerank re-scores and bills through the SAME metering path
            if reranker is not None and retr_rerank is not None:
                assert rep_rerank is not None
                hooks.usage.clear()
                hooks.outcomes.clear()
                rr = await retr_rerank.search(uid, "RAGAS 评估框架的核心指标")
                kinds = {k for _, _, k in hooks.usage}
                print(f"[rerank] outcome={rr.outcome} hits={len(rr.chunks)} "
                      f"usage_kinds={sorted(kinds)} "
                      f"rerank_tokens={[t for _, t, k in hooks.usage if k == 'rerank']}")
                assert "embedding" in kinds, "query embedding was not metered"
                if rr.outcome == OUTCOME_HYBRID_OK:
                    assert "rerank" in kinds, "rerank succeeded but was not metered"
                    assert any(t > 0 for _, t, k in hooks.usage if k == "rerank")
                assert rr.chunks

            # 5) ACL through the REAL retriever (Milvus filter + SQL WHERE)
            other = await retr.search(uid + 1, "RAG 检索质量用哪些指标评估")
            assert other.chunks == [], f"cross-user leak: {other.chunks}"
            print(f"[acl] other-user hits=0 mode={other.mode.value} "
                  f"outcome={other.outcome}")

            # 6) degradation on the real stack, each step reporting its outcome
            #    (Milvus "down" -> BM25 carries it)
            d_hooks = _RecordingHooks()
            d1 = HybridRetriever(
                store, embedder=emb, vector_store=_DownVectorStore(),
                lexical_index=MemoryBM25Index(store), config=cfg, hooks=d_hooks,
            )
            r1 = await d1.search(uid, "评估检索质量的指标 Recall MRR")
            print(f"[degrade:vector-down] mode={r1.mode.value} outcome={r1.outcome} "
                  f"hits={len(r1.chunks)} reported={d_hooks.outcomes}")
            assert r1.mode is RetrievalMode.BM25_FALLBACK
            assert r1.outcome == OUTCOME_BM25_FALLBACK
            assert r1.chunks, "BM25 fallback served nothing on real MySQL rows"
            assert d_hooks.outcomes == [OUTCOME_BM25_FALLBACK]

            #    (embedding endpoint "down" -> embed_failed, nothing billed)
            d2_hooks = _RecordingHooks()
            d2 = HybridRetriever(
                store, embedder=_DownEmbedder(), vector_store=vec,
                lexical_index=MemoryBM25Index(store), config=cfg, hooks=d2_hooks,
            )
            r2 = await d2.search(uid, "评估检索质量的指标 Recall MRR")
            print(f"[degrade:embed-down] outcome={r2.outcome} hits={len(r2.chunks)} "
                  f"billed={d2_hooks.usage}")
            assert r2.outcome == OUTCOME_EMBED_FAILED
            assert r2.chunks
            assert d2_hooks.usage == [], "a failed embed must not bill tokens"

            #    (vector AND lexical down -> SQL LIKE from the truth)
            d3_hooks = _RecordingHooks()
            d3 = HybridRetriever(
                store, embedder=_DownEmbedder(), vector_store=_DownVectorStore(),
                lexical_index=_DownLexical(), config=cfg, hooks=d3_hooks,
            )
            r3 = await d3.search(uid, "RAGAs")
            print(f"[degrade:both-down] mode={r3.mode.value} outcome={r3.outcome} "
                  f"hits={len(r3.chunks)}")
            assert r3.mode is RetrievalMode.SQL_FALLBACK
            assert r3.outcome == OUTCOME_SQL_FALLBACK
            assert r3.chunks, "SQL LIKE found nothing for a literal corpus term"
            assert any("RAGAs" in c.text or "RAGAs" in c.title_path for c in r3.chunks)

            print("\n=== REAL-STACK RETRIEVAL OK ===")
            print(f"recall@5={rep_hybrid.metrics['recall@5']:.3f} "
                  f"mrr={rep_hybrid.metrics['mrr']:.3f} "
                  f"hit@1={rep_hybrid.metrics['hit@1']:.3f} "
                  f"(vector_only recall@5={rep_vec.metrics['recall@5']:.3f}"
                  + (f", +rerank recall@5={rep_rerank.metrics['recall@5']:.3f})"
                     if rep_rerank else ")"))
        finally:
            await store.delete_doc(uid, "rag-eval")
            await vec.drop()  # zero residue: drop the itest collection
            await vec.close()
            await store.dispose()

    asyncio.run(main())
