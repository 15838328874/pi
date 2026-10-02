"""Probe: what rerank ACTUALLY costs in latency, measured against the real endpoint.

Answers the production question "加上重排实践上增加多少延迟、线上能不能接受" with
measurements, not intuition:

  A. Pure rerank micro-benchmark - ONE batched POST per N. The cross-encoder call
     is a single request carrying N (query, doc) pairs, so its cost is O(N) work
     in one round-trip, NOT N round-trips. Chunks are pulled from the live corpus
     so the token counts are the real ones (v2 medical guidelines, p50 ~750 chars).
  B. End-to-end retrieval on the real stack (MySQL truth + Milvus projection +
     real embedding + real reranker): the SAME queries with rerank off vs on,
     paired, so the delta isolates rerank from everything else. Both arms pay the
     same embedding + vector + BM25 + fuse + hydrate costs, so the subtraction is
     clean - and the absolute numbers are honest (no embedding cache).
  C. Query-embedding latency (cache-miss) - the other network hop every query pays,
     which is the fair yardstick for "is rerank's addition material?"

Run: .venv/Scripts/python.exe tools/probe_latency.py
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pi  # noqa: F401,E402  (loads .env from cwd)

from pi.rag.config import RagConfig  # noqa: E402
from pi.rag.defaults.bm25 import MemoryBM25Index  # noqa: E402
from pi.rag.defaults.http_embedder import HttpEmbedder  # noqa: E402
from pi.rag.defaults.http_reranker import HttpReranker  # noqa: E402
from pi.rag.defaults.milvus_vector import MilvusRagVectorStore  # noqa: E402
from pi.rag.defaults.mysql_store import MysqlChunkStore  # noqa: E402
from pi.rag.eval.harness import GoldenSet  # noqa: E402
from pi.rag.retriever import HybridRetriever  # noqa: E402
from pi.rag.types import RetrievedChunk  # noqa: E402

EVAL_USER = 992002
COLLECTION = "pi_rag_chunks_itest_v2"
DB_URL = os.environ.get(
    "PI_ITEST_DATABASE_URL", "mysql+aiomysql://pi:pi_py_local@127.0.0.1:3306/pi_py_test"
)
MILVUS_URI = os.environ.get("PI_ITEST_MILVUS_URI", "http://127.0.0.1:19531")

A_TRIALS = 5      # rerank micro-benchmark repetitions per N
B_ROUNDS = 3      # end-to-end rounds (each round = every query, both arms)
C_TRIALS = 5      # embedding repetitions


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    if not xs:
        return 0.0
    return xs[min(len(xs) - 1, int(len(xs) * p))]


async def part_a(rr: HttpReranker, chunks: list[RetrievedChunk], query: str) -> None:
    print()
    print("=" * 72)
    print("A. rerank micro-benchmark - ONE batched POST per N (real endpoint)")
    print("=" * 72)
    print("   N = number of (query,doc) pairs in the single request; cost is O(N)")
    print(f"   {'N':>4}  {'in_tokens':>9}  {'p50':>8}  {'min':>8}  {'max':>8}   ms/pair")
    for n in (5, 10, 20, 30):
        window = chunks[:n]
        if len(window) < n:
            continue
        lats: list[float] = []
        toks = 0
        for _ in range(A_TRIALS):
            t0 = time.perf_counter()
            await rr.rerank(query, window)
            lats.append((time.perf_counter() - t0) * 1000)
            toks = rr.last_usage.usage_tokens if rr.last_usage else 0
        p50 = statistics.median(lats)
        print(f"   {n:>4}  {toks:>9}  {p50:>7.0f}m  {min(lats):>7.0f}m  {max(lats):>7.0f}m   {p50 / n:>6.1f}")


async def part_b(store, vec, emb, lexical, cfg, queries: list[str]) -> None:
    print()
    print("=" * 72)
    print(f"B. end-to-end retrieval, real stack ({len(queries)} queries x {B_ROUNDS} rounds)")
    print("=" * 72)

    rr = HttpReranker(cfg.rerank_url, cfg.rerank_api_key, cfg.rerank_model)
    r_off = HybridRetriever(store, embedder=emb, vector_store=vec, lexical_index=lexical,
                            reranker=None, config=cfg)
    r_on = HybridRetriever(store, embedder=emb, vector_store=vec, lexical_index=lexical,
                           reranker=rr, config=cfg)

    # warm-up (BM25 lazy build, Milvus/Milvus-TLS handshake, connection pools)
    await r_on.search(EVAL_USER, queries[0], k=5)

    off_lat: list[float] = []
    on_lat: list[float] = []
    paired: list[float] = []
    for _ in range(B_ROUNDS):
        for q in queries:
            t0 = time.perf_counter()
            res_off = await r_off.search(EVAL_USER, q, k=5)
            d_off = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            res_on = await r_on.search(EVAL_USER, q, k=5)
            d_on = (time.perf_counter() - t0) * 1000
            off_lat.append(d_off)
            on_lat.append(d_on)
            paired.append(d_on - d_off)

    print(f"   {'arm':<26}{'p50':>9}{'p95':>9}{'mean':>9}   (ms)")
    print(f"   {'hybrid (no rerank)':<26}{statistics.median(off_lat):>9.0f}"
          f"{pct(off_lat, .95):>9.0f}{statistics.mean(off_lat):>9.0f}")
    print(f"   {'hybrid + rerank (prod)':<26}{statistics.median(on_lat):>9.0f}"
          f"{pct(on_lat, .95):>9.0f}{statistics.mean(on_lat):>9.0f}")
    print(f"   {'paired delta (on - off)':<26}{statistics.median(paired):>9.0f}"
          f"{pct(paired, .95):>9.0f}{statistics.mean(paired):>9.0f}")
    share = statistics.median(paired) / max(1.0, statistics.median(on_lat)) * 100
    print(f"   -> rerank is ~{share:.0f}% of the warm end-to-end retrieval time")
    print(f"   outcome(on): {res_on.outcome}  rerank_tokens/hit: "
          f"{rr.last_usage.usage_tokens if rr.last_usage else 0}")


async def part_c(emb: HttpEmbedder) -> None:
    print()
    print("=" * 72)
    print("C. query embedding latency, cache-miss (the other network hop)")
    print("=" * 72)
    lats: list[float] = []
    for i in range(C_TRIALS):
        t0 = time.perf_counter()
        await emb.embed([f"企业知识库检索探针 query {i}"])
        lats.append((time.perf_counter() - t0) * 1000)
    print(f"   p50={statistics.median(lats):.0f}ms  min={min(lats):.0f}ms  max={max(lats):.0f}ms")


async def part_d(rr: HttpReranker, chunks: list[RetrievedChunk], queries: list[str]) -> None:
    print()
    print("=" * 72)
    print("D. concurrent rerank - throughput view (N=20 pairs per request)")
    print("=" * 72)
    print(f"   {'conc':>4}  {'wall':>9}  {'per-req p50':>12}  {'per-req max':>12}  {'req/s':>7}")
    for conc in (1, 4, 8):
        window = chunks[:20]
        qs = (queries * 2)[:conc]

        async def one(q: str) -> float:
            t0 = time.perf_counter()
            await rr.rerank(q, window)
            return (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        lats = await asyncio.gather(*[one(q) for q in qs])
        wall = (time.perf_counter() - t0) * 1000
        print(f"   {conc:>4}  {wall:>8.0f}m  {statistics.median(lats):>11.0f}m  "
              f"{max(lats):>11.0f}m  {conc / max(0.001, wall / 1000):>6.1f}")


async def main() -> int:
    if not os.environ.get("PI_RAG_RERANK_URL"):
        print("PI_RAG_RERANK_URL not set in .env - nothing to measure")
        return 1
    cfg = RagConfig.from_env()  # real endpoints from .env, like production
    print(f"[cfg] final_k={cfg.retrieval.final_k} rerank_candidates={cfg.retrieval.rerank_candidates} "
          f"vector_k={cfg.retrieval.vector_k} bm25_k={cfg.retrieval.bm25_k}")

    store = MysqlChunkStore(DB_URL, create_schema=True)
    vec = MilvusRagVectorStore(uri=MILVUS_URI, collection=COLLECTION)
    emb = HttpEmbedder(cfg.embedding.url, cfg.embedding.api_key, cfg.embedding.model)
    rr = HttpReranker(cfg.rerank_url, cfg.rerank_api_key, cfg.rerank_model)

    rows = await store.list_chunks_for_user(EVAL_USER)
    if not rows:
        print(f"no chunks for user {EVAL_USER} - v2 corpus not ingested")
        return 1
    print(f"[data] {len(rows)} chunks for user {EVAL_USER} (collection {COLLECTION})")

    # Real chunk text for the micro-benchmark. DESCENDING (longest-first) is the
    # honest direction: `chunks[:n]` slices from the front, so an ascending sort
    # would feed the N SHORTEST chunks and under-report both tokens and latency.
    # That is exactly the bug this line had (comment said longest-first, code
    # sorted ascending) - it made N=20 read 2,707 tokens / 186ms, while the real
    # retrieved top-20 is ~19.3k tokens / ~500ms. Keep it conservative: the
    # longest chunks are the worst case a production query can hand the
    # cross-encoder, and part_b already reports the real retrieved distribution.
    chunks = [RetrievedChunk(chunk_id=int(c.chunk_id), doc_key=c.doc_key, text=c.text,
                             score=0.5, title_path=c.title_path) for c in rows]
    chunks.sort(key=lambda c: len(c.text) + len(c.title_path or ""), reverse=True)
    lens = [len(c.text) + len(c.title_path or "") for c in chunks]
    print(f"[data] chunk chars: p50={pct(lens, .5)} p75={pct(lens, .75)} p90={pct(lens, .9)} "
          f"max={lens[-1]}")

    gs = GoldenSet.load(ROOT / "evals" / "tasks" / "rag" / "corpus_v2.json")
    queries = [c.query for c in gs.cases if c.query][:10]
    print(f"[data] {len(queries)} live queries from corpus_v2 golden set")

    await part_c(emb)
    await part_a(rr, chunks, queries[0])
    lexical = MemoryBM25Index(store)
    await part_b(store, vec, emb, lexical, cfg, queries)
    await part_d(rr, chunks, queries)

    await rr.aclose()
    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
