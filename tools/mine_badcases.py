"""Bad-case miner: read ONE eval case end to end, on the real stack.

Why this exists
---------------
An A/B report tells you WHICH cases moved (``case_diff``) but not WHY. To
attribute a regression you have to read the actual text: the query, the gold
chunk in full, and what each config ranked above it - with scores. Doing that
by hand means re-wiring the whole runtime every time. This tool does the
wiring once and prints the evidence, so a bad case can be judged as one of:

  - a retrieval bug (the gold is there, ranked below junk),
  - a rerank-model preference (cross-encoder genuinely scores a neighbour
    higher - then it is a model property, not our bug),
  - a chunking-boundary artifact (the answer got split across two chunks), or
  - a GOLD-SET defect (the "answer" is a PDF-extraction artifact, the gold
    chunk does not actually contain a clean answer, or another chunk is an
    equally valid answer the golden set did not mark).

That last category matters most: a golden set that encodes a parsing defect
will "fail" forever and send you chasing a retrieval bug that does not exist.
Reading the raw text is the only way to tell these apart - which is exactly
the M4 house rule "promote 前必须赢 per-case diff，而 per-case diff 必须人工读".

Requires an ingested corpus (run ``ab_rag.py --keep`` first, or ingest by hand).

Usage:
    python tools/mine_badcases.py --cases rag-034,rag-036
    python tools/mine_badcases.py --cases rag-034 --configs vector_only,hybrid_rrf60+rerank
    python tools/mine_badcases.py --cases rag-034 --topk 5
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import pi  # noqa: F401,E402  (loads ./.env)

from build_golden_set import select_corpus  # noqa: E402
from pi.rag.config import ChunkingConfig, EmbeddingConfig, RagConfig, RetrievalConfig  # noqa: E402
from pi.rag.defaults.bm25 import MemoryBM25Index  # noqa: E402
from pi.rag.defaults.http_embedder import HttpEmbedder  # noqa: E402
from pi.rag.defaults.http_reranker import HttpReranker  # noqa: E402
from pi.rag.defaults.milvus_vector import MilvusRagVectorStore  # noqa: E402
from pi.rag.defaults.mysql_store import MysqlChunkStore  # noqa: E402
from pi.rag.eval.harness import GoldenSet, chunk_key, rebind_golden  # noqa: E402
from pi.rag.retriever import HybridRetriever  # noqa: E402

_FINAL_K = 5
_CAND_K = 20


def _cfg(*, rerank: bool) -> RagConfig:
    rc = RetrievalConfig(
        vector_k=_CAND_K, bm25_k=_CAND_K, final_k=_FINAL_K,
        rerank_enabled=rerank, rerank_candidates=_CAND_K,
    )
    return RagConfig(chunking=ChunkingConfig(max_chars=800, min_chars=100), retrieval=rc)


def _bar(ch: str = "=") -> str:
    return ch * 78


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True, help="comma-separated case ids, e.g. rag-034,rag-036")
    ap.add_argument("--configs", default="vector_only,hybrid_rrf60,hybrid_rrf60+rerank",
                    help="comma-separated: vector_only | bm25_only | hybrid_rrf60 | hybrid_rrf60+rerank")
    ap.add_argument("--topk", type=int, default=_FINAL_K, help="how many ranked hits to show per config")
    ap.add_argument("--corpus", default="v1", choices=["v1", "v2"],
                    help="which corpus profile to mine (must match the --keep ingest that "
                         "left the corpus in MySQL/Milvus, and the golden set filename)")
    args = ap.parse_args()

    prof = select_corpus(args.corpus)
    EVAL_USER = prof["eval_user"]
    collection = prof["collection"]
    golden_file = prof["golden_file"]

    want_cases = [c.strip() for c in args.cases.split(",") if c.strip()]
    want_cfgs = [c.strip() for c in args.configs.split(",") if c.strip()]

    db_url = os.environ.get("PI_ITEST_DATABASE_URL", "mysql+aiomysql://pi:pi_py_local@127.0.0.1:3306/pi_py_test")
    milvus_uri = os.environ.get("PI_ITEST_MILVUS_URI", "http://127.0.0.1:19531")
    rerank_url = os.environ.get("PI_RAG_RERANK_URL", "")
    rerank_key = os.environ.get("PI_RAG_RERANK_API_KEY", "")
    rerank_model = os.environ.get("PI_RAG_RERANK_MODEL", "")

    emb_cfg = EmbeddingConfig(
        url=os.environ["PI_EMBEDDING_URL"], api_key=os.environ["PI_EMBEDDING_API_KEY"],
        model=os.environ["PI_EMBEDDING_MODEL"], 
    )

    gs_path = ROOT / "evals" / "tasks" / "rag" / golden_file
    golden = GoldenSet.load(gs_path)

    store = MysqlChunkStore(db_url)
    vec = MilvusRagVectorStore(uri=milvus_uri, collection=collection)
    emb = HttpEmbedder(url=emb_cfg.url, api_key=emb_cfg.api_key, model=emb_cfg.model)
    lexical = MemoryBM25Index(store)

    try:
        chunks = await store.list_chunks_for_user(EVAL_USER)
        if not chunks:
            raise SystemExit(
                f"corpus empty for user {EVAL_USER}: run `python tools/ab_rag.py --keep ...` "
                f"first (this tool reads an already-ingested corpus, it does not ingest)."
            )
        by_key = {chunk_key(c.doc_key, c.seq): c for c in chunks}
        id_to_key = {int(c.chunk_id): chunk_key(c.doc_key, c.seq) for c in chunks}

        # Rebind so a chunking drift surfaces as a key change, not a silent miss.
        rebound, rep = rebind_golden(golden, chunks)
        if rep.unresolved or rep.ambiguous:
            print(f"[rebind] {rep.unresolved} unresolved / {rep.ambiguous} ambiguous - "
                  f"corpus moved under the golden set")
        golden = rebound

        cases = {c.id: c for c in golden.cases}
        missing = [c for c in want_cases if c not in cases]
        if missing:
            raise SystemExit(f"case id(s) not in golden set: {missing}")

        # Build the retrievers once, reuse across cases.
        retrs: dict[str, HybridRetriever] = {}
        for name in want_cfgs:
            if name == "vector_only":
                retrs[name] = HybridRetriever(store, embedder=emb, vector_store=vec,
                                              lexical_index=None, config=_cfg(rerank=False))
            elif name == "bm25_only":
                retrs[name] = HybridRetriever(store, embedder=None, vector_store=None,
                                              lexical_index=lexical, config=_cfg(rerank=False))
            elif name == "hybrid_rrf60":
                retrs[name] = HybridRetriever(store, embedder=emb, vector_store=vec,
                                              lexical_index=lexical, config=_cfg(rerank=False))
            elif name == "hybrid_rrf60+rerank":
                if not (rerank_url and rerank_key):
                    raise SystemExit("hybrid_rrf60+rerank needs PI_RAG_RERANK_* in .env")
                rr = HttpReranker(url=rerank_url, api_key=rerank_key, model=rerank_model)
                retrs[name] = HybridRetriever(store, embedder=emb, vector_store=vec,
                                              lexical_index=lexical, reranker=rr,
                                              config=_cfg(rerank=True))
            else:
                raise SystemExit(f"unknown config '{name}'")

        for cid in want_cases:
            case = cases[cid]
            print("\n" + _bar())
            print(f"CASE {cid}   tags={case.tags}   category={case.category}")
            print(_bar())
            print(f"QUERY : {case.query}")
            print(f"GOLD  : {case.gold_chunk_keys}")
            if case.notes:
                print(f"NOTES : {case.notes}")
            print(f"GT    : {case.ground_truth!r}")

            # --- gold chunk(s) in FULL: is the answer even cleanly in there? ---
            for gk in case.gold_chunk_keys:
                gc = by_key.get(gk)
                print("\n" + _bar("-") + f"\nGOLD CHUNK {gk}")
                if not gc:
                    print("  (not in corpus - rebind should have caught this)")
                    continue
                print(f"  title_path: {gc.title_path!r}")
                print(f"  page      : {gc.page}")
                print(f"  text_to_index() (what rerank now sees, = what BM25 indexes):")
                for ln in gc.text_to_index().splitlines() or [""]:
                    print(f"    | {ln}")
                # Is the ground_truth literally inside the indexed text? A gold
                # set whose answer is NOT in its own gold chunk is defective.
                gt = (case.ground_truth or "").strip()
                idx = gc.text_to_index()
                if gt:
                    # normalize the CJK inter-char spaces PDF extraction injects
                    def _sq(s: str) -> str:
                        return "".join(s.split())
                    print(f"  GT substring of gold text?  raw={gt in idx}  "
                          f"space-normalized={_sq(gt) in _sq(idx)}")

            # --- each config's ranking, with scores, gold position marked ---
            for name in want_cfgs:
                retr = retrs[name]
                t0 = time.time()
                hits = await retr.search_chunks(EVAL_USER, case.query, args.topk)
                dt = (time.time() - t0) * 1000
                print("\n" + _bar("-") + f"\nCONFIG {name}   ({dt:.0f}ms)")
                goldset = set(case.gold_chunk_keys)
                for i, h in enumerate(hits, 1):
                    k = id_to_key.get(int(h.chunk_id), f"{h.doc_key}#?")
                    mark = "  <== GOLD" if k in goldset else ""
                    first = (h.text or "").replace("\n", " ")[:70]
                    # Near-1.0 scores print at FULL precision: a fixed .4f would
                    # show several distinct ~0.999999x scores as "+1.0000" and
                    # read as cross-encoder saturation, which measurement showed
                    # does NOT happen (see badcase_v2_lost4_diagnosis.md §
                    # Refuted hypothesis 2). Only the near-1.0 band needs this;
                    # lower scores stay human-readable.
                    sc = f"{h.score!r}" if h.score >= 0.9995 else f"{h.score:+.4f}"
                    print(f"  {i}. score={sc}  {k}{mark}")
                    print(f"       title: {h.title_path[:60]!r}")
                    print(f"       text : {first!r}")
    finally:
        await vec.close()
        await store.dispose()


def _seq_of(hit, by_key) -> int:  # pragma: no cover - kept for ad-hoc REPL use
    """Recover a hit's seq from its chunk_id via the corpus map (hits carry
    chunk_id/doc_key but not seq). The hot path uses the id_to_key dict built
    in main(); this linear scan is only a convenience for interactive poking."""
    for _k, c in by_key.items():
        if int(c.chunk_id) == int(hit.chunk_id):
            return c.seq
    return -1


if __name__ == "__main__":
    asyncio.run(main())
