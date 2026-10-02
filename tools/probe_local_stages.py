"""Probe: where the NON-rerank, NON-embedding retrieval time actually goes.

`probe_latency.py` measures the two remote hops (query embedding, rerank) and the
end-to-end total. Everything else - vector search + BM25 + RRF + hydrate - is
only ever known there BY SUBTRACTION (~355ms p50 on corpus v2). That number is
suspiciously large for an in-process index, a local Milvus query and one SQL
`IN` query, so this probe measures each stage directly instead of inferring it.

It also reproduces the retriever's ACTUAL call order: the vector channel (embed
-> Milvus) and the lexical channel run SEQUENTIALLY today, despite
`retriever.py` claiming "run both channels concurrently". The gap between
`sum` and `max` here is what parallelising them would buy.

Run: .venv/Scripts/python.exe tools/probe_local_stages.py
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
from pi.rag.defaults.milvus_vector import MilvusRagVectorStore  # noqa: E402
from pi.rag.defaults.mysql_store import MysqlChunkStore  # noqa: E402
from pi.rag.eval.harness import GoldenSet  # noqa: E402
from pi.rag.retriever import rrf_fuse  # noqa: E402

EVAL_USER = 992002
COLLECTION = "pi_rag_chunks_itest_v2"
DB_URL = os.environ.get(
    "PI_ITEST_DATABASE_URL", "mysql+aiomysql://pi:pi_py_local@127.0.0.1:3306/pi_py_test"
)
MILVUS_URI = os.environ.get("PI_ITEST_MILVUS_URI", "http://127.0.0.1:19531")

ROUNDS = 3
N_QUERIES = 10


def p50(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def p95(xs: list[float]) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * 0.95))]


async def main() -> int:
    cfg = RagConfig.from_env()
    rc = cfg.retrieval
    print(f"[cfg] vector_k={rc.vector_k} bm25_k={rc.bm25_k} rrf_k={rc.rrf_k} "
          f"rerank_candidates={rc.rerank_candidates} final_k={rc.final_k}")

    store = MysqlChunkStore(DB_URL, create_schema=True)
    vec = MilvusRagVectorStore(uri=MILVUS_URI, collection=COLLECTION)
    emb = HttpEmbedder(cfg.embedding.url, cfg.embedding.api_key, cfg.embedding.model)
    lex = MemoryBM25Index(store)

    rows = await store.list_chunks_for_user(EVAL_USER)
    if not rows:
        print(f"no chunks for user {EVAL_USER} - v2 corpus not ingested")
        return 1
    print(f"[data] {len(rows)} chunks for user {EVAL_USER}")

    gs = GoldenSet.load(ROOT / "evals" / "tasks" / "rag" / "corpus_v2.json")
    queries = [c.query for c in gs.cases if c.query][:N_QUERIES]
    print(f"[data] {len(queries)} live queries from corpus_v2")

    # --- cold vs warm BM25 build (the index is built lazily per user) --------
    t0 = time.perf_counter()
    await lex.search(EVAL_USER, queries[0], int(rc.bm25_k))
    d_bm25_cold = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    await lex.search(EVAL_USER, queries[0], int(rc.bm25_k))
    d_bm25_warm = (time.perf_counter() - t0) * 1000
    print(f"[bm25] cold build+query={d_bm25_cold:.0f}ms  warm query={d_bm25_warm:.0f}ms")

    # warm up the other clients too (TLS / pool / Milvus load)
    e = await emb.embed_query(queries[0])
    await vec.search(EVAL_USER, e.vectors[0], int(rc.vector_k))

    embed_l: list[float] = []
    milvus_l: list[float] = []
    bm25_l: list[float] = []
    rrf_l: list[float] = []
    hydrate_l: list[float] = []
    channel_seq_l: list[float] = []   # today: embed+vec THEN bm25 (sequential)
    channel_par_l: list[float] = []   # if the two channels ran concurrently

    for _ in range(ROUNDS):
        for q in queries:
            t0 = time.perf_counter()
            res = await emb.embed_query(q)
            d_embed = (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            vhits = await vec.search(EVAL_USER, res.vectors[0], int(rc.vector_k))
            d_vec = (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            lhits = await lex.search(EVAL_USER, q, int(rc.bm25_k))
            d_lex = (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            fused = rrf_fuse(
                [[int(c) for c, _ in vhits], [int(c) for c, _ in lhits]],
                k=int(rc.rrf_k),
            )
            d_rrf = (time.perf_counter() - t0) * 1000

            ids = [int(c) for c, _ in fused[: int(rc.rerank_candidates)]]
            t0 = time.perf_counter()
            await store.get_chunks_by_ids(ids)
            d_hyd = (time.perf_counter() - t0) * 1000

            embed_l.append(d_embed)
            milvus_l.append(d_vec)
            bm25_l.append(d_lex)
            rrf_l.append(d_rrf)
            hydrate_l.append(d_hyd)
            # vector channel = embed + milvus (retriever awaits them back to back)
            channel_seq_l.append(d_embed + d_vec + d_lex)
            channel_par_l.append(max(d_embed + d_vec, d_lex))

    def line(name: str, xs: list[float]) -> None:
        print(f"   {name:<34}{p50(xs):>8.0f}{p95(xs):>8.0f}")

    print()
    print("=" * 72)
    print(f"stage breakdown, real stack, {len(embed_l)} samples (warm)")
    print("=" * 72)
    print(f"   {'stage':<34}{'p50':>8}{'p95':>8}   (ms)")
    line("query embedding (remote API)", embed_l)
    line("Milvus vector search", milvus_l)
    line("BM25 search (warm, in-process)", bm25_l)
    line("RRF fuse (pure CPU)", rrf_l)
    line("hydrate (MySQL IN, top-20)", hydrate_l)
    print("   " + "-" * 48)
    line("channels SEQUENTIAL (today)", channel_seq_l)
    line("channels CONCURRENT (max)", channel_par_l)
    saved = p50(channel_seq_l) - p50(channel_par_l)
    print(f"   -> parallelising the two channels would save p50 ~{saved:.0f}ms")

    print()
    print("=" * 72)
    print("reconciliation vs probe_latency.py")
    print("=" * 72)
    local = p50(embed_l) + p50(milvus_l) + p50(bm25_l) + p50(rrf_l) + p50(hydrate_l)
    print(f"   sum of stages (sequential, no rerank) = {local:.0f}ms")
    print(f"   probe_latency measured no-rerank e2e   = 552ms (p50)")
    print(f"   unexplained gap                        = {552 - local:.0f}ms"
          "   <- python/async/serialisation overhead")

    # --- Milvus consistency level: what does read-after-write cost? ---------
    # The store pins consistency_level="Strong" so a chunk is searchable the
    # instant it is written. That is a correctness win, but Strong forces every
    # search to sync against the query coordinator - on a 527-vector collection
    # a brute-force scan takes ~1ms, so if search costs hundreds of ms, the
    # consistency barrier is the cost, not the ANN work. This measures it.
    print()
    print("=" * 72)
    print("Milvus consistency level: cost of read-after-write (same vectors)")
    print("=" * 72)
    vecs: list[list[float]] = []
    for q in queries:
        r = await emb.embed_query(q)
        vecs.append(list(r.vectors[0]))
    client = vec._get_client()
    filt = f"user_id == {int(EVAL_USER)}"
    print(f"   {'level':<14}{'p50':>8}{'p95':>8}{'hits':>7}   (ms)")
    for level in ("Strong", "Bounded", "Session", "Eventually"):
        lats: list[float] = []
        nhits = 0
        for _ in range(2):
            for v in vecs:
                t0 = time.perf_counter()
                res = await asyncio.to_thread(
                    client.search,
                    COLLECTION,
                    data=[v],
                    filter=filt,
                    limit=int(rc.vector_k),
                    output_fields=["chunk_id"],
                    search_params={"metric_type": "COSINE"},
                    consistency_level=level,
                )
                lats.append((time.perf_counter() - t0) * 1000)
                nhits += len(res[0]) if res else 0
        print(f"   {level:<14}{p50(lats):>8.0f}{p95(lats):>8.0f}{nhits:>7}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
