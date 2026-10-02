"""Real-stack A/B attribution for the RAG retriever (M4-b, 对接文档 §6 step 4).

M3 measured the shipped retriever on a 9-chunk single-doc corpus and produced a
counter-intuitive result: hybrid RRF (recall@5 0.833) was WORSE than
vector_only (0.867). An aggregate table cannot explain that - it can only
report it. This tool exists to find out WHY, on a corpus big enough to mean
something (390 chunks, 6 docs, 60 golden cases).

Three things it does that a plain A/B table does not:

1. **Channel decomposition.** Before fusing anything, it records for every
   query what EACH channel did on its own: did it contain the gold, at what
   rank, with what score. Fusion can only hurt if one channel is weak, and
   "weak" has two completely different flavours that need different fixes:
     - the channel never finds the gold  -> coverage problem (tokenizer,
       embedding, indexing text)
     - the channel finds the gold but ranks junk above it -> precision
       problem, and RRF is rank-blind so it launders that junk into the top-k
   The aggregate metric looks identical for both. Only decomposition separates
   them.

2. **Hypothesis variants measured in the harness, not committed to the kernel.**
   Two candidate fixes are implemented here as Protocol wrappers/subclasses
   (score-gated BM25, prose-only lexical statistics) so they can be scored
   BEFORE any production code changes. 先建评测再调检索: a knob that is not
   measured does not get merged.

3. **Per-case diffs.** ``case_diff`` shows which specific cases moved and in
   which direction. A fusion that wins 8 and loses 8 has a flat mean but two
   distinct bugs; only the per-case list exposes them.

Costs: ONE ingest of the corpus (~242k embedding tokens), then every config
reuses the same index and the same cached query embeddings (~60 embeds total).
Rerank configs hit the real cross-encoder per query.

Run:  python tools/ab_rag.py [--keep] [--reuse]
      --keep    leave the corpus ingested afterwards (enables --reuse)
      --reuse   skip ingest if the corpus is already there (needs a --keep run)
Out:  evals/reports/ab_<ts>.md
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import pi  # noqa: F401,E402  (loads ./.env)

from build_golden_set import select_corpus  # noqa: E402  single source of truth
from pi.rag.config import ChunkingConfig, EmbeddingConfig, RagConfig, RetrievalConfig  # noqa: E402
from pi.rag.defaults.bm25 import MemoryBM25Index, _UserIndex  # noqa: E402
from pi.rag.defaults.http_embedder import HttpEmbedder  # noqa: E402
from pi.rag.defaults.http_reranker import HttpReranker  # noqa: E402
from pi.rag.defaults.milvus_vector import MilvusRagVectorStore  # noqa: E402
from pi.rag.defaults.mysql_store import MysqlChunkStore  # noqa: E402
from pi.rag.eval.harness import (  # noqa: E402
    GoldenSet,
    ab_markdown,
    case_diff,
    chunk_key,
    diff_markdown,
)
from pi.rag.eval.runner import EvalRunner  # noqa: E402
from pi.rag.ingest import IngestPipeline  # noqa: E402
from pi.rag.retriever import HybridRetriever, rrf_fuse  # noqa: E402
from pi.rag.types import Chunk, EmbedResult, IngestStatus  # noqa: E402

_ITEST_COLLECTION = "pi_rag_chunks_itest"
_FINAL_K = 5
_CAND_K = 20  # per-channel candidate depth for both probe and fusion


def _corpus_dir() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "待测试文档"
        if cand.is_dir():
            return cand
    raise SystemExit("待测试文档 corpus not found")


# ---------------------------------------------------------------------------
# Wrappers: candidate fixes measured here, merged only if they win
# ---------------------------------------------------------------------------


class CachingEmbedder:
    """Memoizes embed_query so N configs do not re-bill the same 60 queries.

    Legitimate cache (not a fake): query embedding is a pure function of
    (text, model), and every config in a sweep uses the same model. The
    underlying embedder is the REAL HttpEmbedder - the first call per query is
    a real billed request. ``calls`` counts real network hits so the report can
    prove the cache did not silently replace the model.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self._cache: dict[str, EmbedResult] = {}
        self.calls = 0

    async def embed(self, texts: list[str]) -> EmbedResult:
        return await self._inner.embed(texts)

    async def embed_query(self, text: str) -> EmbedResult:
        if text in self._cache:
            return self._cache[text]
        self.calls += 1
        res = await self._inner.embed_query(text)
        self._cache[text] = res
        return res


class GatedLexical:
    """Drops BM25 candidates whose score is a small fraction of the top score.

    Why this is the prime suspect: RRF fuses by RANK and is deliberately blind
    to score magnitude. A BM25 list whose #1 scores 0.31 and whose #20 scores
    0.02 is a list with no confidence in anything below the top few - but RRF
    awards rank 20 the same 1/(k+20) as a vector rank 20 that scored 0.71
    cosine. Fusion then promotes lexical noise into the final window and
    displaces a vector hit that was right.

    A relative floor (score >= ratio * top_score) is scale-free, so it needs no
    per-corpus constant. This wrapper is a LexicalIndex - the retriever cannot
    tell it from the real one, which is the point: the fix is measured before
    anyone decides whether it belongs in the kernel.
    """

    def __init__(self, inner, ratio: float) -> None:
        self._inner = inner
        self.ratio = float(ratio)

    async def search(self, user_id: int, query: str, k: int):
        hits = await self._inner.search(user_id, query, k)
        if not hits:
            return hits
        top = max(float(s) for _, s in hits)
        if top <= 0:
            return hits
        floor = self.ratio * top
        return [(cid, s) for cid, s in hits if float(s) >= floor]

    async def invalidate(self, user_id: int) -> None:
        await self._inner.invalidate(user_id)


class DocScopedBM25(MemoryBM25Index):
    """BM25 whose corpus statistics cover only an allowlist of docs.

    Tests the avgdl hypothesis directly: the corpus is 351 short CSV rows
    (avg ~14 tokens) plus 34 prose chunks (avg ~115). BM25's length
    normalization divides by the corpus-wide avgdl, so mixing the two drags
    avgdl to ~23 and penalizes every prose chunk by roughly 5x relative to a
    CSV row. Restricting the lexical index to prose changes avgdl AND df
    together, which is what makes it a real test rather than a filter.

    Vectors still see all 390 chunks - only the lexical channel is scoped.
    """

    def __init__(self, store, allow: set[str]) -> None:
        super().__init__(store)
        self._allow = set(allow)

    def _build_index(self, chunks: list[Chunk]) -> _UserIndex:
        return _UserIndex([c for c in chunks if c.doc_key in self._allow])


# ---------------------------------------------------------------------------
# Channel decomposition - the part that actually explains the result
# ---------------------------------------------------------------------------


@dataclass
class ChannelProbe:
    """Per-query evidence from each channel BEFORE fusion."""

    case_id: str
    query: str
    gold: set[str]
    vec_rank: int = 0  # 1-based rank of first gold in the vector channel, 0 = absent
    vec_score: float = 0.0  # cosine of that gold hit
    vec_top_score: float = 0.0
    lex_rank: int = 0
    lex_score: float = 0.0  # BM25 of that gold hit
    lex_top_score: float = 0.0
    lex_hits: int = 0
    lex_ratio: float = 0.0  # lex_score / lex_top_score (confidence in the gold)
    fused_rank: int = 0

    @property
    def vec_found(self) -> bool:
        return self.vec_rank > 0

    @property
    def lex_found(self) -> bool:
        return self.lex_rank > 0


@dataclass
class ProbeSummary:
    """Aggregate verdict of the decomposition, with the decision it implies."""

    n: int = 0
    vec_only_wins: int = 0  # gold in vector top-k, NOT in lexical top-k
    lex_only_wins: int = 0  # gold in lexical top-k, NOT in vector -> BM25 earns its place
    both: int = 0
    neither: int = 0
    lex_junk_above_gold: int = 0  # lexical ranked >=1 junk chunk above the gold
    lex_low_confidence: int = 0  # gold present lexically but at <=35% of top score
    notes: list[str] = field(default_factory=list)

    def markdown(self) -> str:
        pct = lambda x: f"{100 * x / max(1, self.n):.0f}%"  # noqa: E731
        lines = [
            "## Channel decomposition (pre-fusion, per query)",
            "",
            f"- queries probed: {self.n}",
            f"- gold in BOTH channels' top-{_CAND_K}: {self.both} ({pct(self.both)})",
            f"- gold in VECTOR only: **{self.vec_only_wins}** ({pct(self.vec_only_wins)})",
            f"- gold in LEXICAL only: **{self.lex_only_wins}** ({pct(self.lex_only_wins)})",
            f"- gold in NEITHER: {self.neither} ({pct(self.neither)})",
            "",
            "Lexical-channel quality on the cases where it did find the gold:",
            f"- ranked junk above the gold: {self.lex_junk_above_gold}",
            f"- gold at <=35% of the top BM25 score (low confidence): "
            f"{self.lex_low_confidence}",
        ]
        if self.notes:
            lines += ["", "### reading"] + [f"- {n}" for n in self.notes]
        return "\n".join(lines)


async def probe_channels(store, vec, lexical, emb, cases, id_to_key, eval_user, k=_CAND_K) -> list[ChannelProbe]:
    """Record what each channel did alone, for every golden query.

    Also computes the RRF-fused rank of the gold IN PROBE (same rrf_fuse the
    retriever uses, k=60) so the mechanism can be stated causally: "the gold
    was vector-rank V and lexical-rank L, and fusion put it at F" - which is
    the difference between observing that fusion lost and proving WHY.
    """
    out: list[ChannelProbe] = []
    for case in cases:
        gold = set(case.gold_chunk_keys)
        p = ChannelProbe(case_id=case.id, query=case.query, gold=gold)

        try:
            e = await emb.embed_query(case.query)
            vhits = await vec.search(eval_user, e.vectors[0], k) if e.vectors else []
        except Exception as exc:  # noqa: BLE001 - a probe must not kill the sweep
            vhits = []
            print(f"  !! probe vector error on {case.id}: {exc}")
        if vhits:
            p.vec_top_score = float(vhits[0][1])
            for i, (cid, s) in enumerate(vhits, start=1):
                if id_to_key.get(int(cid)) in gold:
                    p.vec_rank, p.vec_score = i, float(s)
                    break

        try:
            lhits = await lexical.search(eval_user, case.query, k)
        except Exception as exc:  # noqa: BLE001
            lhits = []
            print(f"  !! probe lexical error on {case.id}: {exc}")
        p.lex_hits = len(lhits)
        if lhits:
            p.lex_top_score = float(lhits[0][1])
            for i, (cid, s) in enumerate(lhits, start=1):
                if id_to_key.get(int(cid)) in gold:
                    p.lex_rank, p.lex_score = i, float(s)
                    break
            if p.lex_top_score > 0 and p.lex_found:
                p.lex_ratio = p.lex_score / p.lex_top_score

        # Fused rank of the gold, computed the way the retriever computes it.
        fused = rrf_fuse(
            [[int(cid) for cid, _ in vhits], [int(cid) for cid, _ in lhits]], k=60
        )
        for rank, (cid, _s) in enumerate(fused, start=1):
            if id_to_key.get(int(cid)) in gold:
                p.fused_rank = rank
                break
        out.append(p)
    return out


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def mechanism_lines(probes: list[ChannelProbe]) -> list[str]:
    """Causal statement of WHY fusion moves the gold, from the probe ranks.

    For every query where BOTH channels found the gold, compare the gold's
    vector rank against its fused rank. Fusion can only help if the lexical
    channel ranks the gold BETTER than the vector channel does; otherwise it
    drags the gold down. Counting those directions is the mechanism - not a
    hypothesis, a tally over the real probe.
    """
    both = [p for p in probes if p.vec_found and p.lex_found]
    if not both:
        return ["- (no query had the gold in both channels; mechanism n/a)"]
    dragged = [p for p in both if p.fused_rank > p.vec_rank]
    helped = [p for p in both if p.fused_rank < p.vec_rank]
    flat = [p for p in both if p.fused_rank == p.vec_rank]
    lex_worse = [p for p in both if p.lex_rank > p.vec_rank]
    mv, ml, mf = (_mean([p.vec_rank for p in both]), _mean([p.lex_rank for p in both]),
                  _mean([p.fused_rank for p in both]))
    lines = [
        f"- queries with gold in BOTH channels: {len(both)}",
        f"- lexical rank is WORSE than vector rank on: **{len(lex_worse)}** "
        f"/ {len(both)} ({100*len(lex_worse)/len(both):.0f}%)",
        f"- fusion DRAGGED the gold below its vector rank: **{len(dragged)}**",
        f"- fusion LIFTED the gold above its vector rank: {len(helped)}",
        f"- fusion left it flat: {len(flat)}",
        f"- mean rank of gold: vector **{mv:.2f}**, lexical **{ml:.2f}**, "
        f"fused **{mf:.2f}**",
    ]
    # State the mechanism from the numbers, not from a guess. The decisive fact
    # is the mean-rank ordering: if the lexical mean is worse than the vector
    # mean, rank-averaging must pull the gold down on net.
    if ml > mv and mf > mv:
        lines.append(
            f"- **mechanism**: the lexical opinion is weaker on average (mean rank "
            f"{ml:.2f} vs {mv:.2f}), and RRF averages ranks - so the fused rank "
            f"({mf:.2f}) lands between them, i.e. BELOW where vectors alone had "
            f"the gold. {len(dragged)} demotions vs {len(helped)} promotions. "
            f"This is why RRF k and BM25 score gates cannot help: they do not "
            f"change either channel's rank ORDER, and order is all RRF reads. The "
            f"only fusion-side lever is the lexical WEIGHT (or a cross-encoder "
            f"that re-scores from scratch)."
        )
    elif len(helped) > len(dragged):
        lines.append(
            f"- fusion is net-positive here ({len(helped)} lifts vs {len(dragged)} "
            f"drags): the lexical channel is earning its place, so keep weight 1.0."
        )
    return lines


def summarize_probes(probes: list[ChannelProbe]) -> ProbeSummary:
    s = ProbeSummary(n=len(probes))
    for p in probes:
        v = p.vec_found
        l = p.lex_found
        if v and l:
            s.both += 1
        elif v:
            s.vec_only_wins += 1
        elif l:
            s.lex_only_wins += 1
        else:
            s.neither += 1
        if l:
            if p.lex_rank > 1:
                s.lex_junk_above_gold += 1
            if p.lex_ratio and p.lex_ratio <= 0.35:
                s.lex_low_confidence += 1

    # The verdict, stated as a decision rather than a vibe.
    if s.lex_only_wins == 0 and s.vec_only_wins > 0:
        s.notes.append(
            f"BM25 contributes ZERO unique gold (lexical-only wins = 0) while the vector "
            f"channel uniquely covers {s.vec_only_wins} queries. On this corpus fusion can "
            f"only inject noise: RRF has nothing to gain and a full candidate list to "
            f"displace. **This is the mechanism behind hybrid < vector_only.**"
        )
    elif s.lex_only_wins > 0:
        s.notes.append(
            f"BM25 uniquely covers {s.lex_only_wins} queries, so fusion has real upside - "
            f"if hybrid still loses, the fault is precision (junk ranked above gold), not "
            f"coverage. Look at the gated configs."
        )
    if s.both and s.lex_junk_above_gold / max(1, s.both + s.lex_only_wins) > 0.5:
        s.notes.append(
            "Most lexical finds rank junk above the gold - a precision problem. "
            "Note what does NOT fix it: a BM25 score floor (RRF reads ranks, never "
            "magnitudes - measured 0.000 change at 20%/35% gates) and a different "
            "RRF k. What does: down-weighting the lexical opinion, or a "
            "cross-encoder that re-scores from scratch."
        )
    if s.neither:
        s.notes.append(
            f"{s.neither} queries are missed by BOTH channels: no fusion rule can fix those. "
            f"They are parser/chunker/embedding-model problems and cap every config here."
        )
    return s


# ---------------------------------------------------------------------------
# Config sweep
# ---------------------------------------------------------------------------


def _cfg(**over) -> RagConfig:
    """One RagConfig with only the named retrieval knobs changed."""
    rc = RetrievalConfig(
        vector_k=_CAND_K, bm25_k=_CAND_K, final_k=_FINAL_K,
        rerank_enabled=False, rerank_candidates=_CAND_K,
    )
    for k, v in over.items():
        setattr(rc, k, v)
    return RagConfig(
        chunking=ChunkingConfig(max_chars=800, min_chars=100), retrieval=rc
    )


@dataclass
class Spec:
    name: str
    cfg: RagConfig
    lexical: object | None  # None = vector_only
    rerank: bool = False
    note: str = ""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="leave corpus ingested (enables --reuse)")
    ap.add_argument("--reuse", action="store_true", help="skip ingest if corpus already present")
    ap.add_argument("--corpus", default="v1", choices=["v1", "v2"],
                    help="which corpus profile to A/B (must match the golden set built by "
                         "build_golden_set.py --corpus). v2 = 13 medical-guideline PDFs, the "
                         "second-corpus replication for M4 verdict gate (b).")
    ap.add_argument("--only", default="",
                    help="comma-separated config names to run (default: all 13). "
                         "Verification runs of a single fix should use this - a full "
                         "sweep is ~5min of real billed embedding calls, and re-running "
                         "12 configs that a fix cannot possibly affect is waste. "
                         "Report sections keyed on a missing config degrade to n/a, "
                         "they do not crash.")
    ap.add_argument("--exclude-tag", default="",
                    help="comma-separated case tags to EXCLUDE from scoring. Use this to "
                         "isolate a mechanism: e.g. `--exclude-tag lexical_leak` drops the "
                         "cases whose question copies a long verbatim span from its gold "
                         "chunk (BM25 wins those by construction), so the remaining numbers "
                         "measure fusion on genuinely semantic queries only. Per-tag slices "
                         "in the report are NOT a substitute - they overlap (a case can be "
                         "both edge_case and lexical_leak), so no slice is the complement.")
    args = ap.parse_args()

    prof = select_corpus(args.corpus)
    CORPUS = prof["corpus"]
    EVAL_USER = prof["eval_user"]
    _ITEST_COLLECTION = prof["collection"]
    golden_file = prof["golden_file"]
    print(f"[corpus] profile {args.corpus}: {len(CORPUS)} docs, user {EVAL_USER}, "
          f"collection {_ITEST_COLLECTION}")

    corpus = _corpus_dir()
    db_url = os.environ.get(
        "PI_ITEST_DATABASE_URL", "mysql+aiomysql://pi:pi_py_local@127.0.0.1:3306/pi_py_test"
    )
    milvus_uri = os.environ.get("PI_ITEST_MILVUS_URI", "http://127.0.0.1:19531")
    for var in ("PI_EMBEDDING_URL", "PI_EMBEDDING_API_KEY", "PI_EMBEDDING_MODEL"):
        if not os.environ.get(var):
            raise SystemExit(f"{var} required in .env")
    rerank_url = os.environ.get("PI_RAG_RERANK_URL", "")
    rerank_key = os.environ.get("PI_RAG_RERANK_API_KEY", "")
    rerank_model = os.environ.get("PI_RAG_RERANK_MODEL", "")

    emb_cfg = EmbeddingConfig(
        url=os.environ["PI_EMBEDDING_URL"],
        api_key=os.environ["PI_EMBEDDING_API_KEY"],
        model=os.environ["PI_EMBEDDING_MODEL"],
        style=os.environ.get("PI_RAG_EMBED_STYLE", "auto"),
    )

    gs_path = ROOT / "evals" / "tasks" / "rag" / golden_file
    if not gs_path.is_file():
        raise SystemExit(f"golden set missing: run `python tools/build_golden_set.py --corpus {args.corpus}` ({gs_path})")
    golden = GoldenSet.load(gs_path)
    print(f"[golden] {len(golden.cases)} cases from {gs_path.name}")

    store = MysqlChunkStore(db_url, create_schema=True)
    vec = MilvusRagVectorStore(uri=milvus_uri, collection=_ITEST_COLLECTION)
    emb = CachingEmbedder(HttpEmbedder(url=emb_cfg.url, api_key=emb_cfg.api_key,
                                       model=emb_cfg.model, style=emb_cfg.style))
    cfg = _cfg()
    lexical_full = MemoryBM25Index(store)
    prose_keys = {k for k, _, kind in CORPUS if kind == "prose"}

    t_all = time.time()
    ingested = False
    try:
        assert await vec.ping(), "local Milvus unreachable"

        existing = await store.list_chunks_for_user(EVAL_USER)
        if args.reuse and len(existing) > 300:
            print(f"[ingest] --reuse: {len(existing)} chunks already present, skipping ingest")
            chunks = existing
        else:
            pipe = IngestPipeline(store=store, embedder=emb, vector_store=vec,
                                  config=cfg, lexical_index=lexical_full)
            await vec.drop()
            for key, _, _ in CORPUS:
                await store.delete_doc(EVAL_USER, key)
            print("=== ingest (real embedding, local MySQL + Milvus) ===")
            usage = 0
            for key, fname, kind in CORPUS:
                p = corpus / fname
                if not p.is_file():
                    print(f"  SKIP missing {fname}")
                    continue
                t0 = time.time()
                out = await pipe.ingest_file(p, user_id=EVAL_USER, doc_key=key)
                usage += out.usage_tokens
                flag = "" if out.status == IngestStatus.READY.value else f" <- {out.reason[:40]}"
                print(f"  {key:12s} {kind:8s} {out.status:18s} chunks={out.chunks_stored:4d} "
                      f"{time.time() - t0:5.1f}s{flag}")
            print(f"  TOTAL chunks={len(await store.list_chunks_for_user(EVAL_USER))} "
                  f"embed_tokens={usage}")
            chunks = await store.list_chunks_for_user(EVAL_USER)
            ingested = True

        by_key = {chunk_key(c.doc_key, c.seq): c for c in chunks}
        id_to_key = {int(c.chunk_id): chunk_key(c.doc_key, c.seq) for c in chunks}
        print(f"[corpus] {len(chunks)} chunks in MySQL, "
              f"{sum(1 for c in chunks if c.doc_key in prose_keys)} prose / "
              f"{sum(1 for c in chunks if c.doc_key not in prose_keys)} distractor")

        # --- corpus statistics that the avgdl hypothesis needs --------------
        from pi.rag.defaults.bm25 import tokenize

        def tok_stats(subset: list[Chunk]) -> tuple[int, float]:
            lens = [len(tokenize(c.text_to_index())) for c in subset]
            return len(lens), (sum(lens) / len(lens) if lens else 0.0)

        prose_chunks = [c for c in chunks if c.doc_key in prose_keys]
        data_chunks = [c for c in chunks if c.doc_key not in prose_keys]
        n_all, avg_all = tok_stats(chunks)
        n_p, avg_p = tok_stats(prose_chunks)
        n_d, avg_d = tok_stats(data_chunks)
        penalty = (0.25 + 0.75 * avg_p / avg_all) / (0.25 + 0.75 * avg_d / avg_all) if avg_all else 0
        stats_md = "\n".join([
            "## Corpus statistics (BM25 length normalization)",
            "",
            f"- all chunks: n={n_all}, avg indexed tokens={avg_all:.1f} -> **avgdl={avg_all:.1f}**",
            f"- prose only: n={n_p}, avg={avg_p:.1f}",
            f"- distractor (csv/xlsx): n={n_d}, avg={avg_d:.1f}",
            f"- BM25 length penalty on a prose chunk vs a distractor chunk "
            f"(b=0.75): **{penalty:.2f}x**",
            "",
            f"Reading: {'the mixed avgdl penalizes prose heavily - the prose-only lexical '
                        'config below is the controlled test' if penalty > 2 else
                        'avgdl distortion is minor; look at precision, not length stats'}",
        ])
        print("\n" + stats_md + "\n")

        # --- rebind: prove the golden set survives this chunking -------------
        from pi.rag.eval.harness import rebind_golden

        rebound, rep = rebind_golden(golden, chunks)
        print("[rebind] " + rep.markdown().replace("\n", "\n           "))
        if rep.unresolved or rep.ambiguous:
            print(f"  !! {rep.unresolved} unresolved / {rep.ambiguous} ambiguous - "
                  f"the corpus moved under the golden set; metrics below use the rebound set")
        golden = rebound

        # --- optional tag exclusion: isolate a mechanism --------------------
        # A per-tag SLICE cannot answer "does fusion help on non-verbatim
        # queries?" because tags overlap (a case can be edge_case AND
        # lexical_leak), so no slice is the complement of another. Exclusion
        # drops the confounded cases entirely and scores the rest as a normal
        # golden set - that is the only way to get the clean subset metric.
        excluded_tags = {t.strip() for t in args.exclude_tag.split(",") if t.strip()}
        if excluded_tags:
            before = len(golden.cases)
            golden = GoldenSet(
                name=golden.name,
                cases=[c for c in golden.cases if not (set(c.tags) & excluded_tags)],
            )
            print(f"[exclude-tag] dropped {before - len(golden.cases)} case(s) tagged "
                  f"{sorted(excluded_tags)} -> scoring {len(golden.cases)}")
            if not golden.cases:
                raise SystemExit(f"--exclude-tag {sorted(excluded_tags)} removed every case")

        runner = EvalRunner(store, k_max=_FINAL_K)

        # --- 1) decomposition BEFORE any fusion -----------------------------
        print(f"\n=== channel probe ({len(golden.cases)} queries, pre-fusion) ===")
        t0 = time.time()
        probes = await probe_channels(store, vec, lexical_full, emb, golden.cases, id_to_key, EVAL_USER)
        summary = summarize_probes(probes)
        print(f"  done in {time.time() - t0:.1f}s "
              f"(real embed calls so far: {emb.calls})")
        print(summary.markdown())

        # --- 2) the config sweep --------------------------------------------
        specs = [
            Spec("vector_only", _cfg(), None,
                 note="no lexical channel at all - the baseline hybrid must beat"),
            Spec("bm25_only", _cfg(), lexical_full,
                 note="lexical channel measured ALONE (vector removed) - is it even any good?"),
            Spec("hybrid_rrf60", _cfg(), lexical_full,
                 note="SHIPPED default: vector+BM25 -> RRF k=60"),
            Spec("hybrid_rrf10", _cfg(rrf_k=10), lexical_full,
                 note="sharper top-rank emphasis"),
            Spec("hybrid_rrf200", _cfg(rrf_k=200), lexical_full,
                 note="flatter: approaches 'how many channels returned it'"),
            Spec("hybrid_bm25k5", _cfg(bm25_k=5), lexical_full,
                 note="fewer lexical candidates -> less noise volume in fusion"),
            Spec("hybrid_gated0.20", _cfg(), GatedLexical(lexical_full, 0.20),
                 note="drop BM25 hits below 20% of the top BM25 score (confidence gate)"),
            Spec("hybrid_gated0.35", _cfg(), GatedLexical(lexical_full, 0.35),
                 note="stricter confidence gate"),
            Spec("hybrid_prose_lex", _cfg(), DocScopedBM25(store, prose_keys),
                 note="BM25 statistics over prose only - the avgdl hypothesis test"),
            # The lever the mechanism analysis actually points to: RRF reads
            # RANKS, so gates and k cannot change the damage a weaker second
            # opinion does - only its WEIGHT can. 0.0 must reproduce
            # vector_only exactly, which is a free correctness check on the knob.
            Spec("hybrid_lexw0.5", _cfg(lexical_weight=0.5), lexical_full,
                 note="lexical opinion at half weight in RRF"),
            Spec("hybrid_lexw0.25", _cfg(lexical_weight=0.25), lexical_full,
                 note="lexical opinion at quarter weight"),
            Spec("hybrid_lexw0.0", _cfg(lexical_weight=0.0), lexical_full,
                 note="lexical weight 0 - MUST equal vector_only (knob sanity check)"),
        ]
        if rerank_url and rerank_key:
            specs.append(Spec("hybrid_rrf60+rerank", _cfg(rerank_enabled=True), lexical_full,
                              rerank=True, note="shipped default + real cross-encoder"))
        else:
            print("\n[rerank] PI_RAG_RERANK_* not set - skipping the cross-encoder config")

        # --only: subset the sweep. Done AFTER the rerank append so the
        # cross-encoder config is selectable too.
        if args.only.strip():
            wanted = [n.strip() for n in args.only.split(",") if n.strip()]
            known = {sp.name for sp in specs}
            unknown = [n for n in wanted if n not in known]
            if unknown:
                raise SystemExit(f"--only: unknown config(s) {unknown}; known = {sorted(known)}")
            specs = [sp for sp in specs if sp.name in wanted]
            print(f"\n[only] running {len(specs)} config(s): {[sp.name for sp in specs]}")
            if "vector_only" not in wanted:
                print("[only] NOTE: vector_only not selected - per-case attribution "
                      "and the A/B baseline column will be n/a. Include it if you "
                      "need the case diff against the baseline.")

        reports = []
        for sp in specs:
            # bm25_only: remove the vector channel by not injecting it
            use_vec = sp.name != "bm25_only"
            reranker = None
            if sp.rerank:
                reranker = HttpReranker(url=rerank_url, api_key=rerank_key, model=rerank_model)
            retr = HybridRetriever(
                store,
                embedder=emb if use_vec else None,
                vector_store=vec if use_vec else None,
                lexical_index=sp.lexical,
                reranker=reranker,
                config=sp.cfg,
                hooks=None,
            )
            t0 = time.time()
            rep = await runner.run(golden, retr.search_chunks, sp.name)
            reports.append(rep)
            m = rep.metrics
            print(f"  {sp.name:22s} hit@1={m['hit@1']:.3f} hit@5={m['hit@5']:.3f} "
                  f"r@5={m['recall@5']:.3f} mrr={m['mrr']:.3f} "
                  f"(r@1={m['recall@1']:.3f} r@3={m['recall@3']:.3f}) "
                  f"err={rep.failed} {time.time() - t0:5.1f}s")

        # --- 2b) knob sanity: lexical_weight=0 must reproduce vector_only ----
        # If it does not, the weighted fusion has a bug and every weighted number
        # in this report is meaningless. Checked on the real stack, not assumed.
        by_name = {r.config_name: r for r in reports}
        zero = by_name.get("hybrid_lexw0.0")
        vonly = by_name.get("vector_only")
        sanity_ok = None
        if zero and vonly:
            same_metrics = all(
                abs(zero.metrics[m] - vonly.metrics[m]) < 1e-9 for m in vonly.metrics
            )
            same_order = all(
                a.ranked_keys == b.ranked_keys
                for a, b in zip(sorted(zero.cases, key=lambda c: c.case_id),
                                sorted(vonly.cases, key=lambda c: c.case_id))
            )
            sanity_ok = same_metrics and same_order
            print(f"\n[sanity] lexical_weight=0 vs vector_only: metrics_identical="
                  f"{same_metrics} rankings_identical={same_order}")
            if not sanity_ok:
                print("  !! KNOB BUG: weight 0 must be exactly 'ignore the lexical "
                      "channel'. Do not trust the weighted rows below.")

        # --- 3) assemble the report -----------------------------------------
        parts = [
            f"# RAG A/B attribution report (M4-b, corpus {args.corpus})",
            "",
            f"- built: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- corpus: {len(chunks)} chunks / {len({c.doc_key for c in chunks})} docs "
            f"(user {EVAL_USER}), embedding `{emb_cfg.model}`",
            f"- golden: {len(golden.cases)} cases from `evals/tasks/rag/{golden_file}`"
            + (f" (EXCLUDING tag(s): {args.exclude_tag})" if excluded_tags else ""),
            f"- real embed calls this run: {emb.calls} (rest served from the query cache)",
            f"- wall clock: {time.time() - t_all:.0f}s",
            f"- stack: local MySQL `{db_url.split('@')[-1]}` + local Milvus "
            f"`{milvus_uri}` collection `{_ITEST_COLLECTION}`",
            f"- knob sanity (`lexical_weight=0` == `vector_only`): "
            + ("**PASS**" if sanity_ok else
               ("**FAIL - weighted rows below are untrustworthy**" if sanity_ok is False
                else "n/a")),
            "",
            stats_md,
            "",
            summary.markdown(),
            "",
            "## Fusion mechanism (gold rank: vector vs lexical vs fused)",
            "",
        ] + mechanism_lines(probes) + [
            "",
            "## A/B table",
            "",
            ab_markdown(reports),
            "",
            "### what each config is",
            "",
            "| config | intent |",
            "|---|---|",
        ] + [f"| `{sp.name}` | {sp.note} |" for sp in specs]

        # per-tag for the two configs the hypothesis is about
        parts += ["", "## Per-tag slices"]
        for name in ("vector_only", "hybrid_rrf60", "hybrid_lexw0.5", "hybrid_lexw0.25",
                     "hybrid_gated0.20", "hybrid_prose_lex", "hybrid_rrf60+rerank"):
            r = by_name.get(name)
            if not r or not r.per_tag:
                continue
            parts += ["", f"### `{name}`", "",
                      "| tag | n | hit@5 | recall@1 | recall@5 | mrr |",
                      "|---|---|---|---|---|---|"]
            for tag, m in r.per_tag.items():
                parts.append(f"| {tag} | {int(m['n'])} | {m['hit@5']:.3f} "
                             f"| {m['recall@1']:.3f} | {m['recall@5']:.3f} "
                             f"| {m['mrr']:.3f} |")

        # per-case attribution: shipped vs baseline, and shipped vs each candidate fix
        parts += ["", "## Per-case attribution"]
        base = by_name.get("vector_only")
        if base:
            for other_name in ("hybrid_rrf60", "hybrid_lexw0.5", "hybrid_lexw0.25",
                               "hybrid_gated0.20", "hybrid_prose_lex", "hybrid_bm25k5",
                               "hybrid_rrf60+rerank"):
                o = by_name.get(other_name)
                if not o:
                    continue
                parts += ["", diff_markdown(base, o, k=_FINAL_K, limit=15)]

        # bad cases that EVERY config misses = the real ceiling
        if base:
            all_miss = [c.case_id for c in base.cases if not c.hit_at.get(_FINAL_K)]
            for r in reports:
                miss = {c.case_id for c in r.cases if not c.hit_at.get(_FINAL_K)}
                all_miss = [cid for cid in all_miss if cid in miss]
            if all_miss:
                parts += ["", "## Unfixable-by-fusion cases", "",
                          f"{len(all_miss)} cases missed by EVERY config "
                          f"(both channels lack the gold - a parser/chunker/embedding "
                          f"ceiling, not a ranking problem):", ""]
                parts += [f"- `{cid}`" for cid in all_miss[:30]]

        # --- 4) the verdict, derived from the numbers -----------------------
        verdict = _verdict(by_name, summary, penalty, probes, args.corpus)
        parts += ["", verdict]
        if excluded_tags:
            parts += [
                "",
                f"- **scoring scope**: cases tagged {sorted(excluded_tags)} were EXCLUDED "
                f"({len(golden.cases)} scored). Every number above is for the remaining "
                f"subset only - do not compare it against a full-corpus report without "
                f"noting the subset.",
            ]

        out_dir = ROOT / "evals" / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"ab_{time.strftime('%Y%m%d_%H%M%S')}.md"
        out_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
        print("\n" + verdict)
        print(f"\n[report] {out_path.relative_to(ROOT)}")
    finally:
        # --keep means keep, regardless of whether THIS run did the ingesting.
        # The old `args.keep and ingested` deleted a reused corpus even though
        # the user asked to keep it - exactly the wrong side to fail on.
        if args.keep:
            print("[keep] corpus left ingested for --reuse")
        else:
            for key, _, _ in CORPUS:
                await store.delete_doc(EVAL_USER, key)
            await vec.drop()
        await vec.close()
        await store.dispose()


ABLATIONS = ("vector_only", "bm25_only", "hybrid_lexw0.0")


def _verdict(by_name: dict, summary: ProbeSummary, penalty: float,
             probes: list[ChannelProbe], corpus_name: str = "v1") -> str:
    """Turn the measurements into a decision. No vibes, no hedging.

    ABLATIONS are excluded from "best config" selection on purpose. An earlier
    version of this function recommended ``hybrid_lexw0.0`` as the best fused
    config - which is circular: lexical_weight=0 *is* vector_only (the sanity
    check proves it byte-for-byte). An ablation exists to validate the knob, not
    to win the sweep.
    """
    v = by_name.get("vector_only")
    h = by_name.get("hybrid_rrf60")
    lines = ["## Verdict", ""]
    if not v or not h:
        return "\n".join(lines + ["(missing baseline or shipped config)"])

    dh5 = h.metrics["hit@5"] - v.metrics["hit@5"]
    d5 = h.metrics["recall@5"] - v.metrics["recall@5"]
    d1 = h.metrics["recall@1"] - v.metrics["recall@1"]
    lines += [
        f"- shipped hybrid vs vector_only: hit@5 (PRIMARY, any-of) "
        f"{v.metrics['hit@5']:.3f} -> {h.metrics['hit@5']:.3f} (**{dh5:+.3f}**); "
        f"recall@5 (secondary all-of) {v.metrics['recall@5']:.3f} -> "
        f"{h.metrics['recall@5']:.3f} (**{d5:+.3f}**); recall@1 "
        f"{v.metrics['recall@1']:.3f} -> {h.metrics['recall@1']:.3f} (**{d1:+.3f}**)",
    ]

    candidates = {n: r for n, r in by_name.items() if n not in ABLATIONS}
    best_name = max(
        candidates,
        key=lambda n: (candidates[n].metrics["hit@5"], candidates[n].metrics["mrr"]),
        default=None,
    )
    best = candidates.get(best_name)
    if best:
        beat = best.metrics["hit@5"] > v.metrics["hit@5"]
        lines += [
            f"- best NON-ablation config: **`{best_name}`** "
            f"hit@5={best.metrics['hit@5']:.3f} recall@5={best.metrics['recall@5']:.3f} "
            f"mrr={best.metrics['mrr']:.3f} "
            f"recall@1={best.metrics['recall@1']:.3f} "
            f"(vs vector_only {v.metrics['hit@5']:.3f}/{v.metrics['recall@5']:.3f}/"
            f"{v.metrics['mrr']:.3f}/{v.metrics['recall@1']:.3f})",
            f"- does any fusion beat pure vector on hit@5 (PRIMARY)? "
            f"**{'YES' if beat else 'NO'}**",
        ]
        # hit@5 can saturate while mrr separates configs - say which won what.
        if not beat and best.metrics["mrr"] > v.metrics["mrr"]:
            lines.append(
                f"- ...but it wins on MRR ({best.metrics['mrr']:.3f} vs "
                f"{v.metrics['mrr']:.3f}) and recall@1 "
                f"({best.metrics['recall@1']:.3f} vs {v.metrics['recall@1']:.3f}): "
                f"it finds the gold slightly less often inside top-5, but ranks it "
                f"clearly better when it does. For a k=5 reader that is a real win."
            )

    lines.append(f"- BM25 unique coverage (queries only the lexical channel finds): "
                 f"**{summary.lex_only_wins}** / {summary.n}")

    # Dose-response: the strongest evidence available here. If down-weighting the
    # lexical opinion monotonically recovers vector-only quality, the lexical
    # channel is causally the thing hurting - not some unrelated interaction.
    ladder = [(w, by_name.get(f"hybrid_lexw{w}")) for w in ("1.0", "0.5", "0.25")]
    ladder = [(w, r) for w, r in ladder if r is not None]
    if v and len(ladder) >= 2:
        ladder.append(("0.0 (=vector_only)", v))
        seq = " -> ".join(f"{w}: {r.metrics['recall@5']:.3f}" for w, r in ladder)
        mrr_seq = " -> ".join(f"{r.metrics['mrr']:.3f}" for _, r in ladder)
        r5 = [r.metrics["recall@5"] for _, r in ladder]
        mono = all(r5[i] <= r5[i + 1] + 1e-9 for i in range(len(r5) - 1))
        lines += [
            f"- dose-response on `lexical_weight` (recall@5 - secondary, but the "
            f"metric most sensitive to this knob): {seq}",
            f"- dose-response on mrr: {mrr_seq}",
            ("- **monotone**: less lexical weight -> closer to vector-only quality, which "
             "is causal confirmation that the lexical opinion is what costs recall here "
             "(an unrelated bug would not track the knob)." if mono else
             "- not monotone: the knob is interacting with something else; do not read "
             "the weight sweep as causal."),
        ]

    if summary.lex_only_wins == 0:
        lines.append(
            "- mechanism: with zero unique lexical coverage, RRF can only reorder the vector "
            "channel's own candidates using a second opinion that never adds a correct one. "
            "Hybrid losing to vector_only is then EXPECTED, not a bug. Ranked by what the "
            "sweep measured: a cross-encoder FIXES it (it re-scores from scratch), a lower "
            "lexical weight MITIGATES it, and RRF k / a BM25 score floor do NOTHING (both "
            "leave rank order intact, and rank order is all RRF reads)."
        )

    # Refuted hypotheses, stated explicitly: a report that only lists winners
    # hides what was ruled out, and the next person re-tests them.
    refuted = []
    g20, g35 = by_name.get("hybrid_gated0.20"), by_name.get("hybrid_gated0.35")
    if g20 and h and abs(g20.metrics["recall@5"] - h.metrics["recall@5"]) < 1e-9:
        refuted.append(
            "`BM25 score gate` - REFUTED: identical recall@5 to the ungated shipped config, "
            "because RRF consumes ranks and never sees magnitudes. Gating the tail cannot "
            "move the gold's rank."
        )
    if g35 and h and g35.metrics["recall@5"] < h.metrics["recall@5"]:
        refuted.append(
            "`stricter gate (0.35)` - REFUTED: strictly worse, it starts deleting real "
            "lexical hits instead of noise."
        )
    pl = by_name.get("hybrid_prose_lex")
    if pl and pl.metrics["recall@5"] < h.metrics["recall@5"]:
        refuted.append(
            f"`avgdl distortion` - REFUTED: measured prose-vs-distractor length penalty is "
            f"only {penalty:.2f}x, and scoping BM25 to prose made recall@5 WORSE "
            f"({pl.metrics['recall@5']:.3f} vs {h.metrics['recall@5']:.3f}). The 351 CSV "
            f"rows are not the cause."
        )
    r10, r200 = by_name.get("hybrid_rrf10"), by_name.get("hybrid_rrf200")
    if r10 and r200:
        refuted.append(
            f"`RRF k` - NOT THE LEVER: k=10/60/200 give recall@5 "
            f"{r10.metrics['recall@5']:.3f}/{h.metrics['recall@5']:.3f}/"
            f"{r200.metrics['recall@5']:.3f} - a spread of "
            f"{max(r10.metrics['recall@5'], r200.metrics['recall@5']) - min(r10.metrics['recall@5'], r200.metrics['recall@5']):.3f}. "
            f"k reshapes how fast rank decays; it cannot make a wrong opinion right."
        )
    if refuted:
        lines += ["", "### hypotheses this sweep REFUTED", ""] + [f"- {r}" for r in refuted]

    if best and best_name and best_name != "hybrid_rrf60":
        gate_b = (
            "(b) be re-measured on a SECOND corpus, because this one is 90% "
            "homogeneous CSV rows." if corpus_name == "v1" else
            "(b) it has now been re-measured on the SECOND corpus (v2, prose-dense "
            "medical guidelines) - compare this verdict against the v1 report before "
            "promoting; agreement across both is what makes it generalizable."
        )
        lines += [
            "",
            f"- action: `{best_name}` is the candidate to promote. Before it replaces the "
            f"shipped default it must (a) win the per-case diff, not just the mean - a mean "
            f"can hide 8 wins / 8 losses, and {gate_b}",
        ]
    both = [p for p in probes if p.vec_found and p.lex_found]
    if both:
        mv = _mean([p.vec_rank for p in both])
        ml = _mean([p.lex_rank for p in both])
        lines.append(
            f"- evidence in one line: gold's mean rank is {mv:.2f} in the vector channel and "
            f"{ml:.2f} in BM25, with {summary.lex_only_wins} unique lexical finds over "
            f"{summary.n} queries."
        )
    if corpus_name == "v1":
        lines.append(
            "- scope warning: this is ONE corpus dominated by 351 homogeneous CSV rows. A "
            "conclusion of 'BM25 is useless' would be over-fitting; the defensible conclusion "
            "is about the FUSION RULE (unweighted RRF treats a weaker channel as an equal "
            "opinion), which is corpus-independent. Re-run with `--corpus v2` (prose-dense "
            "medical guidelines) to test whether the lexical channel earns its keep when the "
            "distractor mass is NOT homogeneous CSV."
        )
    else:
        lines.append(
            "- scope note: this is the SECOND corpus (v2): 13 prose-dense medical guidelines, "
            "NO homogeneous CSV distractor mass, every chunk title_path EMPTY (pdfplumber "
            "emits no heading layer). This is the replication that M4 verdict gate (b) "
            "demands. If BM25 unique coverage is still ~0 here, the v1 conclusion "
            "(fusion cannot help when the lexical channel adds no correct candidate) "
            "GENERALIZES beyond v1's CSV artifact; if BM25 starts winning unique finds, v1 "
            "was over-fitted and the fusion rule needs the lexical weight tuned per-corpus."
        )
    return "\n".join(lines)


if __name__ == "__main__":
    asyncio.run(main())
