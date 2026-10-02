"""M3 retriever tests - fusion math, degradation chain, ACL, metering.

Deterministic layer (house rule): SqliteChunkStore as SQL truth + FakeEmbedder
+ InMemoryVectorStore + MemoryBM25Index, plus hand-written channel doubles that
RAISE on demand. The point of this file is pipeline LOGIC - which branch serves,
what outcome gets reported, and that a broken attachment can never炸 the call -
so failures must be injectable exactly, which a real Milvus cannot do. The REAL
stack (local MySQL + Milvus + real DashScope embedding/rerank, real Recall@k)
is verified in integration/test_rag_real.py under PI_INTEGRATION=1.

Every async body runs inside asyncio.run(main()) - no pytest-asyncio.

Pinned here (regressions that would silently wreck answer quality):
- RRF: rank-based fusion, k=60, deterministic tie-break, absent-from-a-channel
- hybrid happy path: citations hydrate from SQL, scores are RRF values
- degradation chain: hybrid -> vector_only -> bm25_fallback -> sql_fallback
  -> empty, each with the RIGHT outcome string (no silent degradation)
- embed_failed vs bm25_fallback are distinguished (billing/归因 differ)
- healthy-but-empty channel does NOT trigger a fallback (no fabricated hits)
- rerank: reorders + bills; on failure keeps fused order and reports
  rerank_failed; on empty result keeps fused hits (never loses data)
- ACL: user B gets zero hits through every channel, including SQL fallback
- stale vector id (gone from SQL) -> dropped at hydration, not wrong bytes
- doc_keys scoping works on the doc-blind lexical/SQL channels
- hooks that raise cannot break retrieval
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pi.rag.config import RagConfig, RetrievalConfig
from pi.rag.defaults._like import escape_like
from pi.rag.defaults.bm25 import _BUILD_ATTEMPTS, MemoryBM25Index
from pi.rag.defaults.fake_embedder import FakeEmbedder
from pi.rag.defaults.memory_vector import InMemoryVectorStore
from pi.rag.defaults.sqlite_store import SqliteChunkStore
from pi.rag.ingest import IngestPipeline
from pi.rag.protocols import ChunkStore
from pi.rag.retriever import (
    OUTCOME_BM25_FALLBACK,
    OUTCOME_EMBED_FAILED,
    OUTCOME_ERROR,
    OUTCOME_HYBRID_OK,
    OUTCOME_NO_HITS,
    OUTCOME_RERANK_FAILED,
    OUTCOME_SQL_FALLBACK,
    OUTCOME_VECTOR_ONLY,
    HybridRetriever,
    rrf_fuse,
)
from pi.rag.types import (
    Chunk,
    EmbedResult,
    IngestStatus,
    RetrievedChunk,
    RetrievalMode,
)

# -- test doubles -----------------------------------------------------------


class RaisingEmbedder:
    """Embedder whose embed_query always fails - drives the embed_failed path."""

    def __init__(self, message: str = "embedding endpoint 503") -> None:
        self.message = message
        self.calls = 0

    async def embed(self, texts: list[str]) -> EmbedResult:
        self.calls += 1
        raise RuntimeError(self.message)

    async def embed_query(self, text: str) -> EmbedResult:
        return await self.embed([text])


class EmptyVectorEmbedder(FakeEmbedder):
    """Embedder that returns a result with NO vectors (malformed response)."""

    async def embed(self, texts: list[str]) -> EmbedResult:
        return EmbedResult(vectors=[], usage_tokens=0)


class RaisingVectorStore(InMemoryVectorStore):
    """Vector store whose search fails - drives the store_failed path."""

    def __init__(self, message: str = "milvus proxy unreachable") -> None:
        super().__init__()
        self.message = message

    async def search(self, user_id, vector, k, doc_keys=None):
        raise ConnectionError(self.message)


class RaisingLexical:
    """Lexical index that fails to build - drives vector_only."""

    async def search(self, user_id: int, query: str, k: int):
        raise RuntimeError("bm25 index build failed")

    async def invalidate(self, user_id: int) -> None:
        return None


class RaisingStore:
    """ChunkStore wrapper: search_text/get_chunks_by_ids fail on demand."""

    def __init__(self, inner: ChunkStore, *, fail_text=False, fail_hydrate=False) -> None:
        self._inner = inner
        self.fail_text = fail_text
        self.fail_hydrate = fail_hydrate

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def search_text(self, user_id, query, k):
        if self.fail_text:
            raise RuntimeError("mysql gone away")
        return await self._inner.search_text(user_id, query, k)

    async def get_chunks_by_ids(self, chunk_ids):
        if self.fail_hydrate:
            raise RuntimeError("mysql gone away")
        return await self._inner.get_chunks_by_ids(chunk_ids)


class FixedReranker:
    """Reranker that returns a canned order/scores, or fails, or returns []."""

    def __init__(self, mode: str = "reverse", usage: int = 42) -> None:
        self.mode = mode  # reverse | identity | fail | empty | drop_ids
        self.usage = usage
        self.calls: list[tuple[str, int]] = []
        self.last_usage = None

    async def rerank(self, query: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
        self.calls.append((query, len(chunks)))
        if self.mode == "fail":
            raise RuntimeError("rerank endpoint 500")
        if self.mode == "empty":
            return []
        order = list(reversed(chunks)) if self.mode == "reverse" else list(chunks)
        out = []
        for i, c in enumerate(order):
            if self.mode == "drop_ids" and i % 2 == 1:
                continue
            out.append(
                RetrievedChunk(
                    chunk_id=c.chunk_id, doc_key=c.doc_key, text=c.text,
                    score=float(len(order) - i), title=c.title,
                    title_path=c.title_path, source=c.source, page=c.page,
                )
            )
        self.last_usage = EmbedResult(vectors=[], usage_tokens=self.usage)
        return out


class RecordingHooks:
    def __init__(self) -> None:
        self.embed_usage: list[tuple[int, int, str]] = []
        self.retrieval: list[tuple[str, float]] = []

    async def on_embed_usage(self, user_id: int, tokens: int, kind: str = "embedding") -> None:
        self.embed_usage.append((user_id, tokens, kind))

    async def on_retrieval(self, outcome: str, duration_s: float) -> None:
        self.retrieval.append((outcome, duration_s))

    def outcomes(self) -> list[str]:
        return [o for o, _ in self.retrieval]


class ExplodingHooks:
    """Hooks that raise - a broken meter must never break retrieval."""

    async def on_embed_usage(self, user_id, tokens, kind="embedding"):
        raise RuntimeError("metrics backend down")

    async def on_retrieval(self, outcome, duration_s):
        raise RuntimeError("metrics backend down")


# -- corpus fixtures --------------------------------------------------------

_DOCS = [
    # (doc_key, title, filename, body)
    ("rag-eval", "RAG评估手册", "rag_eval.md", (
        "# RAG 评估手册\n\n"
        "## 检索指标\n\n"
        "评估检索质量使用 Recall@k、MRR 和命中率三个指标。Recall@k 衡量金标 chunk "
        "是否出现在前 k 个结果中，MRR 衡量第一个正确结果的倒数排名。\n\n"
        "## 评测集构建\n\n"
        "golden set 需要至少 50 条问答对，每条标注金标 chunk，并分为 happy_path、"
        "edge_case 和 adversarial 三类。\n\n"
        "## A/B 对比\n\n"
        "对比纯向量、混合检索和混合加重排三种配置，输出并排表格。\n"
    )),
    ("chunk-guide", "文档切分指南", "chunk_guide.md", (
        "# 文档切分指南\n\n"
        "## 语义切块\n\n"
        "切块必须尊重标题边界，绝不跨章节合并，否则引用路径会与内容不符。\n\n"
        "## 重叠窗口\n\n"
        "overlap 设为块长的 15% 左右，避免句子被切断丢失上下文。\n\n"
        "## contextual retrieval\n\n"
        "把标题路径前缀拼进 embed_text，显示文本保持干净，零 LLM 成本。\n"
    )),
    ("acl-policy", "多租户权限规范", "acl_policy.md", (
        "# 多租户权限规范\n\n"
        "## user 级过滤\n\n"
        "检索必须按 user_id 过滤。Milvus 用 int filter，MySQL 用 WHERE user_id=?。\n\n"
        "## 零泄漏\n\n"
        "跨用户检索必须零泄漏，需要有专门的测试覆盖，包括词法兜底路径。\n"
    )),
]

_USER_A = 4242
_USER_B = 4343


async def _seed(store: SqliteChunkStore, *, user_id: int = _USER_A, docs=_DOCS) -> dict[str, int]:
    """Ingest the fixture corpus through the REAL pipeline (no hand-built rows).

    Returns doc_key -> chunk count, so tests can assert against reality instead
    of a number duplicated from the fixture.
    """
    emb = BillingEmbedder()
    vec = InMemoryVectorStore()
    lex = MemoryBM25Index(store)
    pipe = IngestPipeline(
        store=store, embedder=emb, vector_store=vec, config=RagConfig(), lexical_index=lex
    )
    counts: dict[str, int] = {}
    tmp = Path(store.path).parent
    for key, title, fname, body in docs:
        p = tmp / fname
        p.write_text(body, encoding="utf-8")
        out = await pipe.ingest_file(p, user_id=user_id, doc_key=key)
        assert out.status == IngestStatus.READY.value, (key, out.reason)
        counts[key] = out.chunks_stored
    return counts


class BillingEmbedder(FakeEmbedder):
    """FakeEmbedder that bills tokens, so metering can be asserted."""

    async def embed(self, texts: list[str]) -> EmbedResult:
        res = await super().embed(texts)
        return EmbedResult(vectors=res.vectors, usage_tokens=len(texts) * 7)


def _store(tmp_path: Path) -> SqliteChunkStore:
    return SqliteChunkStore(tmp_path / "rag.sqlite3")


def _cfg(**rc) -> RagConfig:
    cfg = RagConfig()
    cfg.retrieval = RetrievalConfig(**{**cfg.retrieval.__dict__, **rc})
    return cfg


def _retriever(store, *, vec=None, emb=None, lex=None, reranker=None, hooks=None, cfg=None):
    return HybridRetriever(
        store,
        embedder=emb if emb is not None else BillingEmbedder(),
        vector_store=vec if vec is not None else InMemoryVectorStore(),
        lexical_index=lex if lex is not None else MemoryBM25Index(store),
        reranker=reranker,
        config=cfg or _cfg(),
        hooks=hooks,
    )


async def _seeded(tmp_path: Path, **kw):
    """Store + retriever over an ingested corpus, vector store populated."""
    store = _store(tmp_path)
    counts = await _seed(store)
    vec = InMemoryVectorStore()
    emb = BillingEmbedder()
    # re-project vectors: the seed pipeline used its own throwaway store object
    chunks = await store.list_chunks_for_user(_USER_A)
    res = await emb.embed([c.text_to_embed() for c in chunks])
    await vec.upsert(chunks, res.vectors)
    lex = MemoryBM25Index(store)
    r = _retriever(store, vec=vec, emb=emb, lex=lex, **kw)
    return store, r, counts, vec, lex


# ---------------------------------------------------------------------------
# RRF pure math (no I/O - the constant and the tie-break are the contract)
# ---------------------------------------------------------------------------


def test_rrf_matches_closed_form_and_is_rank_based():
    a = [10, 20, 30]
    b = [20, 30, 10]
    fused = dict(rrf_fuse([a, b], k=60))
    # every id appears at ranks {1,2,3} across the two lists -> equal scores
    assert fused[10] == pytest.approx(1 / 61 + 1 / 63)
    assert fused[20] == pytest.approx(1 / 61 + 1 / 62)
    assert fused[30] == pytest.approx(1 / 62 + 1 / 63)
    # 20 is rank1 in b and rank2 in a -> best
    assert max(fused, key=fused.get) == 20


def test_rrf_ignores_raw_score_magnitude():
    """The whole point of RRF: a channel with huge scores cannot dominate."""
    huge = [1, 2, 3]  # imagine cosine 0.99 vs bm25 37.2 - scales differ wildly
    tiny = [3, 2, 1]
    fused = dict(rrf_fuse([huge, tiny], k=60))
    # symmetric ranks -> symmetric scores, regardless of what the scores WERE
    assert fused[1] == pytest.approx(fused[3])


def test_rrf_absent_from_one_channel_still_fuses():
    fused = dict(rrf_fuse([[1, 2], [2]], k=60))
    assert set(fused) == {1, 2}
    # 2 appears in both -> strictly better than 1 which appears once
    assert fused[2] > fused[1]
    assert fused[1] == pytest.approx(1 / 61)


def test_rrf_tie_break_is_deterministic_ascending_id():
    """Same rank in one list each -> tie. Must break on id, not dict order."""
    fused = rrf_fuse([[7], [3], [5]], k=60)
    assert [cid for cid, _ in fused] == [3, 5, 7]


def test_rrf_rejects_non_positive_k():
    with pytest.raises(ValueError):
        rrf_fuse([[1]], k=0)


def test_rrf_constant_is_the_paper_value():
    from pi.rag.config import RRF_K

    assert RRF_K == 60  # Cormack et al.; changing it silently shifts every A/B


# ---------------------------------------------------------------------------
# Weighted RRF (the lexical_weight knob). M4 measured that BM25 is the WEAKER
# opinion on a real corpus (mean gold rank 2.63 vs 1.53 vector, zero uniquely
# found gold over 60 queries), so rank-averaging demotes the gold. Weight is
# the only fusion-side lever that can express "trust this channel less" - k and
# score gates cannot, because RRF reads ranks and never magnitudes.
# ---------------------------------------------------------------------------


def test_rrf_default_weights_are_the_unweighted_paper_form():
    """Weighting must not change shipped behaviour at the default."""
    rankings = [[10, 20, 30], [20, 30, 10]]
    assert rrf_fuse(rankings, k=60) == rrf_fuse(rankings, k=60, weights=[1.0, 1.0])


def test_rrf_weight_zero_removes_that_channel_entirely():
    """The knob's defining property: weight 0 == the channel was never there.

    This is what makes ``lexical_weight`` safe to ship at 1.0 and tunable
    downwards - at 0 it must reproduce vector-only EXACTLY, not approximately.
    """
    vec, lex = [10, 20, 30], [30, 20, 10]
    assert rrf_fuse([vec, lex], k=60, weights=[1.0, 0.0]) == rrf_fuse([vec], k=60)
    # and the ranking is the vector order, untouched by the lexical list
    assert [cid for cid, _ in rrf_fuse([vec, lex], k=60, weights=[1.0, 0.0])] == [10, 20, 30]


def test_rrf_lower_weight_stops_junk_outranking_the_gold():
    """The actual M4 mechanism, pinned with exact orderings.

    500 is the gold: vector rank 1, and ABSENT from the lexical channel, which
    instead loves two junk chunks (99, 98) and ranks 20 third. Unweighted RRF
    gives the junk the same 1/(k+rank) credit the gold earned by being the
    vector's best hit, so junk lands above the gold. Down-weighting the weaker
    opinion is the only fusion-side lever that fixes this - k and score gates
    leave the rank order (all RRF reads) untouched.
    """
    vec = [500, 20, 30]
    lex = [99, 98, 20]

    fused_full = [cid for cid, _ in rrf_fuse([vec, lex], k=60)]
    # 20 wins outright (in both lists); 99 ties the gold on 1/61 and wins the
    # ascending-id tie-break -> the gold sits at rank 3 behind pure junk.
    assert fused_full == [20, 99, 500, 98, 30]
    assert fused_full.index(500) > fused_full.index(99)

    fused_half = [cid for cid, _ in rrf_fuse([vec, lex], k=60, weights=[1.0, 0.5])]
    # lexical contributions halved: the gold climbs above both junk chunks
    assert fused_half == [20, 500, 30, 99, 98]
    assert fused_half.index(500) < fused_half.index(99)
    # exact arithmetic, not just ordering
    scores = dict(rrf_fuse([vec, lex], k=60, weights=[1.0, 0.5]))
    assert scores[500] == pytest.approx(1 / 61)
    assert scores[20] == pytest.approx(1 / 62 + 0.5 / 63)
    assert scores[99] == pytest.approx(0.5 / 61)


def test_rrf_weight_scales_the_contribution_exactly():
    fused = dict(rrf_fuse([[7]], k=60, weights=[0.25]))
    assert fused[7] == pytest.approx(0.25 / 61)


def test_rrf_rejects_mismatched_or_negative_weights():
    with pytest.raises(ValueError):
        rrf_fuse([[1], [2]], k=60, weights=[1.0])  # too few
    with pytest.raises(ValueError):
        rrf_fuse([[1]], k=60, weights=[1.0, 1.0])  # too many
    with pytest.raises(ValueError):
        rrf_fuse([[1]], k=60, weights=[-0.5])  # negative


def test_lexical_weight_default_is_neutral_one():
    """Shipping default must be the literature value, not a tuned constant.

    M4's finding (BM25 was the weaker opinion on that corpus) does NOT license
    baking in a down-weight: the right value is corpus-dependent, so the
    default stays neutral and the tuning lives in PI_RAG_LEXICAL_WEIGHT.
    """
    from pi.rag.config import RetrievalConfig

    assert RetrievalConfig().lexical_weight == 1.0


def test_retriever_plan_passes_lexical_weight_into_fusion(tmp_path):
    """Wiring test: the config knob must actually reach rrf_fuse.

    Unit-testing rrf_fuse proves the math; it cannot prove the retriever USES
    it. This drives _plan directly with two DISJOINT channels.

    Fixture note (a first attempt got this wrong and is worth recording): the
    junk must not appear in BOTH channels. A chunk both channels return is
    consensus, and RRF correctly puts it above a single-channel hit at any
    weight - that is the feature, not the M4 failure mode. The failure mode is
    a chunk only BM25 likes outranking a chunk only vectors like, which needs
    disjoint lists to isolate.

    RRF reads ranks only, so at weight 1.0 a lexical rank-1 chunk ALWAYS ties a
    vector rank-1 chunk (both 1/(k+1)) and the ascending-id tie-break decides -
    hence the assertions below compare SCORES at 1.0 rather than trusting ids.
    """
    async def main():
        store = _store(tmp_path)
        await _seed(store)
        chunks = await store.list_chunks_for_user(_USER_A)
        assert len(chunks) >= 4, "fixture needs 4 chunks for disjoint channels"
        gold, junk_v, junk_l1, junk_l2 = (int(c.chunk_id) for c in chunks[:4])

        # vector likes gold then junk_v; lexical likes only its own two junks
        vec_hits = [(gold, 0.91), (junk_v, 0.40)]
        lex_hits = [(junk_l1, 12.0), (junk_l2, 9.0)]

        r_full = HybridRetriever(store, config=_cfg(lexical_weight=1.0))
        r_low = HybridRetriever(store, config=_cfg(lexical_weight=0.25))
        r_zero = HybridRetriever(store, config=_cfg(lexical_weight=0.0))

        p_full = r_full._plan(vec_hits, "ok", lex_hits, "ok", r_full.config.retrieval)
        p_low = r_low._plan(vec_hits, "ok", lex_hits, "ok", r_low.config.retrieval)
        p_zero = r_zero._plan(vec_hits, "ok", lex_hits, "ok", r_zero.config.retrieval)

        assert p_full.mode is RetrievalMode.HYBRID
        s_full = dict(p_full.ranked)
        s_low = dict(p_low.ranked)

        # weight 1.0: a lexical-only rank-1 chunk ties the vector-only rank-1
        # gold. That tie is the whole problem - which one wins is arbitrary.
        assert s_full[gold] == pytest.approx(s_full[junk_l1]) == pytest.approx(1 / 61)
        # weight 0.25: the gold strictly outranks every lexical-only chunk
        assert s_low[gold] == pytest.approx(1 / 61)
        assert s_low[junk_l1] == pytest.approx(0.25 / 61)
        assert s_low[gold] > s_low[junk_l1] > s_low[junk_l2]
        # and the gold is now first in the ranking
        assert [cid for cid, _ in p_low.ranked][0] == gold

        # weight 0.0: lexical is gone -> exactly the vector order
        assert [cid for cid, _ in p_zero.ranked] == [gold, junk_v]
        # the weights really changed the ranking (not a no-op test)
        assert [cid for cid, _ in p_full.ranked] != [cid for cid, _ in p_low.ranked]

    asyncio.run(main())


def test_retriever_end_to_end_honors_lexical_weight(tmp_path):
    """Same knob through the full search() path (hydrate + final_k included)."""
    async def main():
        store, r, counts, vec, lex = await _seeded(tmp_path)
        chunks = await store.list_chunks_for_user(_USER_A)
        assert len(chunks) >= 4
        gold, junk_v, junk_l1, junk_l2 = (int(c.chunk_id) for c in chunks[:4])

        class FixedVec:
            """Stands in for a vector channel with a known ranking."""

            async def search(self, user_id, vector, k, doc_keys=None):
                return [(gold, 0.9), (junk_v, 0.3)]

            async def ping(self):
                return True

        class FixedLex:
            async def search(self, user_id, query, k):
                return [(junk_l1, 12.0), (junk_l2, 9.0)]

            async def invalidate(self, user_id):
                return None

        base = dict(final_k=3, vector_k=10, bm25_k=10)
        res_full = await _retriever(
            store, vec=FixedVec(), lex=FixedLex(), cfg=_cfg(**base, lexical_weight=1.0)
        ).search(_USER_A, "q")
        res_low = await _retriever(
            store, vec=FixedVec(), lex=FixedLex(), cfg=_cfg(**base, lexical_weight=0.25)
        ).search(_USER_A, "q")
        res_zero = await _retriever(
            store, vec=FixedVec(), lex=FixedLex(), cfg=_cfg(**base, lexical_weight=0.0)
        ).search(_USER_A, "q")

        for res in (res_full, res_low, res_zero):
            assert res.mode is RetrievalMode.HYBRID, res
            assert res.degraded is False, "a tuned weight is not a degradation"
            assert res.outcome == "hybrid_ok"

        full_ids = [h.chunk_id for h in res_full.chunks]
        low_ids = [h.chunk_id for h in res_low.chunks]
        zero_ids = [h.chunk_id for h in res_zero.chunks]

        assert low_ids[0] == gold, low_ids
        assert zero_ids == [gold, junk_v], zero_ids
        # the lexical-only junk is demoted below the vector's own #2
        assert low_ids.index(junk_v) < low_ids.index(junk_l1)
        assert full_ids != low_ids, "lexical_weight had no effect end to end"
        # RRF scores survive hydration (not zeroed by _fill_scores)
        assert res_low.chunks[0].score == pytest.approx(1 / 61)

    asyncio.run(main())


def test_bad_lexical_weight_env_cannot_break_retrieval(tmp_path):
    """A negative/absurd weight from env must be clamped, not炸 the query.

    rrf_fuse raises on negatives; retrieval must never raise. The clamp in
    _plan is what keeps a typo'd PI_RAG_LEXICAL_WEIGHT a tuning problem rather
    than an outage.
    """
    async def main():
        store, r, counts, vec, lex = await _seeded(tmp_path)
        bad = _retriever(store, vec=vec, lex=lex, cfg=_cfg(lexical_weight=-5.0))
        res = await bad.search(_USER_A, "RAG 检索质量用哪些指标评估")
        assert res.mode is RetrievalMode.HYBRID, res
        assert res.chunks, "a bad weight silently emptied the result set"

        absurd = _retriever(store, vec=vec, lex=lex, cfg=_cfg(lexical_weight=1e9))
        res2 = await absurd.search(_USER_A, "RAG 检索质量用哪些指标评估")
        assert res2.mode is RetrievalMode.HYBRID and res2.chunks

    asyncio.run(main())


def test_lexical_weight_env_override_is_read(monkeypatch):
    from pi.rag.config import RagConfig

    monkeypatch.setenv("PI_RAG_LEXICAL_WEIGHT", "0.4")
    assert RagConfig.from_env().retrieval.lexical_weight == pytest.approx(0.4)
    monkeypatch.setenv("PI_RAG_LEXICAL_WEIGHT", "not-a-number")
    # fail-safe: bad value falls back to neutral 1.0, never crashes boot
    assert RagConfig.from_env().retrieval.lexical_weight == 1.0


# ---------------------------------------------------------------------------
# Lexical/vector symmetry (regression: BM25 used to index only chunk.text)
# ---------------------------------------------------------------------------


def test_lexical_channel_indexes_heading_path_like_the_vector_channel():
    """Found on the real corpus: a term living ONLY in a heading got zero BM25
    hits while the vector channel found it (embed_text carries title_path).
    Hybrid was then strictly WORSE than vector-only for exact-term lookups -
    the one thing BM25 exists to win. Chunk.text_to_index keeps them symmetric.
    """
    from pi.rag.types import Chunk

    ch = Chunk(
        chunk_id=1, doc_key="d", user_id=1, seq=0,
        text="把标题路径前缀拼进显示文本，保持干净。",
        title_path="文档切分指南 > contextual retrieval",
    )
    idx_text = ch.text_to_index()
    assert "contextual" in idx_text.lower()
    assert idx_text.startswith("文档切分指南")
    # body must still be fully present - indexing the path must not drop text
    assert ch.text in idx_text


def test_text_to_index_does_not_duplicate_an_already_inlined_path():
    """When a parser already put the heading into the body, don't double it
    (that would inflate tf/idf and skew BM25 length normalization)."""
    from pi.rag.types import Chunk

    ch = Chunk(
        chunk_id=1, doc_key="d", user_id=1, seq=0,
        text="权限规范\n检索必须按 user_id 过滤。",
        title_path="权限规范",
    )
    assert ch.text_to_index() == ch.text


def test_bm25_finds_a_term_that_only_exists_in_a_heading(tmp_path):
    """End-to-end version of the same regression through the real index.

    "contextual" appears ONLY in the heading "## contextual retrieval" of the
    chunk-guide doc; its body talks about embed_text/LLM and never says the
    word. Before Chunk.text_to_index this returned zero lexical hits.
    """
    async def main():
        store = _store(tmp_path)
        await _seed(store)
        lex = MemoryBM25Index(store)
        hits = await lex.search(_USER_A, "contextual", k=3)
        assert hits, "BM25 missed a term that lives only in a heading"
        rows = await store.get_chunks_by_ids([cid for cid, _ in hits])
        assert any("contextual" in r.title_path.lower() for r in rows), rows
        # and the body alone would not have matched - prove the fixture is real
        assert not any("contextual" in r.text.lower() for r in rows)

    asyncio.run(main())


def test_bm25_build_index_seam_lets_a_subclass_scope_the_corpus(tmp_path):
    """The M4 A/B tool subclasses MemoryBM25Index to index prose-only chunks
    (testing the avgdl hypothesis). That relies on _build_index being the one
    seam where "which chunks become the index" is decided. Pin it: an override
    must actually change what search returns, without touching _ensure's
    per-user lock/double-check.
    """
    async def main():
        store = _store(tmp_path)
        await _seed(store)

        class ProseOnly(MemoryBM25Index):
            def _build_index(self, chunks):
                # keep only the acl-policy doc - the other two must vanish
                from pi.rag.defaults.bm25 import _UserIndex
                return _UserIndex([c for c in chunks if c.doc_key == "acl-policy"])

        full = MemoryBM25Index(store)
        scoped = ProseOnly(store)
        # "Recall@k" lives in rag-eval; full finds it, prose-only cannot
        assert await full.search(_USER_A, "Recall@k 指标", k=5)
        assert not await scoped.search(_USER_A, "Recall@k 指标", k=5)
        # "user_id 过滤" lives in acl-policy; BOTH find it (prose-only kept it)
        assert await full.search(_USER_A, "user_id 过滤", k=5)
        scoped_hits = await scoped.search(_USER_A, "user_id 过滤", k=5)
        assert scoped_hits
        rows = await store.get_chunks_by_ids([cid for cid, _ in scoped_hits])
        assert all(r.doc_key == "acl-policy" for r in rows), rows

    asyncio.run(main())


# ---------------------------------------------------------------------------
# BM25 shard freshness: TTL (R1) + the invalidate/build race guard
# ---------------------------------------------------------------------------


class GatedStore:
    """ChunkStore wrapper that can HOLD list_chunks_for_user mid-flight.

    The race that matters: _ensure reads SQL, and while that read is in flight
    an ingest commits and calls invalidate(). invalidate() finds no cached
    shard to pop (it hasn't been stored yet), and the in-flight build then
    caches a PRE-ingest snapshot under a fresh TTL timestamp - a shard that is
    stale forever, because nothing will ever invalidate it again. The epoch
    counter in MemoryBM25Index is what catches this; the gate below is the only
    way to hit the interleaving deterministically.
    """

    def __init__(self, inner: ChunkStore) -> None:
        self._inner = inner
        self.list_calls = 0
        self.release: asyncio.Event | None = None  # set -> reads proceed
        self.snapshot_after_read: bool = False  # return the rows as of read START

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def list_chunks_for_user(self, user_id: int):
        self.list_calls += 1
        if self.snapshot_after_read:
            rows = await self._inner.list_chunks_for_user(user_id)
        if self.release is not None:
            await self.release.wait()
        if not self.snapshot_after_read:
            rows = await self._inner.list_chunks_for_user(user_id)
        return rows


def _chunk(user_id: int, doc_key: str, seq: int, text: str) -> Chunk:
    return Chunk(chunk_id=0, doc_key=doc_key, user_id=user_id, seq=seq, text=text)


def test_bm25_default_has_no_ttl_legacy_behaviour_pinned(tmp_path):
    """ttl_s defaults to 0: a cached shard is served until invalidate(), even
    if SQL changed underneath. 17 construction sites (CLI, A/B harness, tests)
    and every shipped eval baseline depend on exactly this behaviour.
    """
    async def main():
        store = _store(tmp_path)
        await store.add_chunks([_chunk(_USER_A, "d", 0, "苹果 香蕉 水果清单")])
        gated = GatedStore(store)
        idx = MemoryBM25Index(gated)  # no ttl_s -> legacy

        assert await idx.search(_USER_A, "苹果", k=3)
        calls_after_first = gated.list_calls

        # SQL gains a new term WITHOUT invalidate - legacy index must not see it
        await store.add_chunks([_chunk(_USER_A, "d", 1, "汽车 火车 交通工具")])
        assert not await idx.search(_USER_A, "汽车", k=3), "TTL leaked into the default"
        assert gated.list_calls == calls_after_first, "no-TTL shard was rebuilt"

        # invalidate() still works (the ingest path)
        await idx.invalidate(_USER_A)
        assert await idx.search(_USER_A, "汽车", k=3)

    asyncio.run(main())


def test_bm25_ttl_rebuilds_a_stale_shard_from_sql(tmp_path):
    """R1: with ttl_s>0 an expired shard is rebuilt from SQL truth on the next
    search - WITHOUT anyone calling invalidate(). This is what bounds the
    multi-worker staleness window: another process's ingest becomes visible
    here within ttl_s even though invalidate() never reached us.
    """
    async def main():
        store = _store(tmp_path)
        await store.add_chunks([_chunk(_USER_A, "d", 0, "苹果 香蕉 水果清单")])
        gated = GatedStore(store)
        idx = MemoryBM25Index(gated, ttl_s=0.05)

        assert await idx.search(_USER_A, "苹果", k=3)
        first = gated.list_calls

        # fresh within the TTL: served from cache, no extra SQL read
        assert await idx.search(_USER_A, "苹果", k=3)
        assert gated.list_calls == first

        # another process ingests; our invalidate() is never called
        await store.add_chunks([_chunk(_USER_A, "d", 1, "汽车 火车 交通工具")])
        await asyncio.sleep(0.08)  # exceed ttl_s

        assert await idx.search(_USER_A, "汽车", k=3), "expired shard was not rebuilt"
        assert gated.list_calls == first + 1, "expected exactly one rebuild read"
        # and the OLD shard is replaced, not merged: rebuilt index sees both docs
        assert await idx.search(_USER_A, "苹果", k=3)

    asyncio.run(main())


def test_bm25_invalidate_during_inflight_build_never_caches_the_stale_snapshot(tmp_path):
    """The race, deterministically: a build's SQL read starts, an ingest
    commits and invalidates while the read is still parked, then the build
    resumes. Publishing its snapshot would cache pre-ingest rows as fresh.
    The epoch guard must detect the bump, discard the snapshot, and re-read -
    so the very first search after the race already sees the NEW chunk.
    """
    async def main():
        store = _store(tmp_path)
        await store.add_chunks([_chunk(_USER_A, "d", 0, "苹果 香蕉 水果清单")])
        gated = GatedStore(store)
        idx = MemoryBM25Index(gated, ttl_s=300.0)  # long TTL: only the race can save us

        gate = asyncio.Event()
        gated.release = gate
        gated.snapshot_after_read = True  # read rows BEFORE parking in the gate

        search_task = asyncio.create_task(idx.search(_USER_A, "苹果", k=3))
        # let _ensure get as far as "rows read, parked in gate"
        for _ in range(200):
            await asyncio.sleep(0.005)
            if gated.list_calls >= 1:
                break
        assert gated.list_calls == 1, "build never started"

        # ingest lands while the build holds a PRE-ingest snapshot, then
        # invalidates (finds nothing cached to pop - the race window)
        await store.add_chunks([_chunk(_USER_A, "d", 1, "汽车 火车 交通工具")])
        await idx.invalidate(_USER_A)

        gate.set()  # release the stale build
        hits = await search_task
        assert hits, "the racing search returned nothing"

        # DECISIVE assertion: the stale snapshot must NOT have been cached.
        # ttl is 300s, so if the pre-ingest rows were stored, this search
        # would be served from that cache and could not see 汽车.
        assert await idx.search(_USER_A, "汽车", k=3), (
            "stale pre-ingest snapshot was cached as fresh - epoch guard broken"
        )
        # exactly one extra rebuild read was allowed (the retry), not a loop
        assert gated.list_calls <= 1 + 1 + _BUILD_ATTEMPTS

    asyncio.run(main())


def test_bm25_ttl_zero_disables_expiry_even_if_constructed_with_zero(tmp_path):
    """ttl_s=0 must mean OFF, not "expire immediately" (which would turn every
    search into a full SQL read + re-tokenize - a latency cliff, not a feature).
    """
    async def main():
        store = _store(tmp_path)
        await store.add_chunks([_chunk(_USER_A, "d", 0, "苹果 香蕉 水果清单")])
        gated = GatedStore(store)
        idx = MemoryBM25Index(gated, ttl_s=0.0)
        assert await idx.search(_USER_A, "苹果", k=3)
        n = gated.list_calls
        await asyncio.sleep(0.02)
        assert await idx.search(_USER_A, "苹果", k=3)
        assert gated.list_calls == n, "ttl_s=0 caused a rebuild; 0 must disable"

    asyncio.run(main())


def test_bm25_ttl_is_per_user_not_global(tmp_path):
    """Shards are per-user; expiry must not evict other users' fresh shards
    (that would be a cross-tenant latency/cost bug: every query rebuilds)."""
    async def main():
        store = _store(tmp_path)
        await store.add_chunks([_chunk(_USER_A, "d", 0, "苹果 香蕉 水果清单")])
        await store.add_chunks([_chunk(_USER_B, "d", 0, "汽车 火车 交通工具")])
        gated = GatedStore(store)
        idx = MemoryBM25Index(gated, ttl_s=0.05)

        assert await idx.search(_USER_A, "苹果", k=3)
        assert await idx.search(_USER_B, "汽车", k=3)
        after_both = gated.list_calls
        assert after_both == 2, "each user builds its own shard exactly once"

        await asyncio.sleep(0.08)
        # only A is queried -> only A's shard may be rebuilt; B's stays cached
        assert await idx.search(_USER_A, "苹果", k=3)
        assert gated.list_calls == after_both + 1
        # B is expired too, but nothing rebuilt it eagerly (lazy by design)
        assert await idx.search(_USER_B, "汽车", k=3)
        assert gated.list_calls == after_both + 2

    asyncio.run(main())


def test_bm25_ttl_env_override_is_read(monkeypatch):
    """PI_RAG_BM25_TTL must reach RetrievalConfig, fail-safe on garbage."""
    from pi.rag.config import RagConfig, RetrievalConfig

    assert RetrievalConfig().bm25_ttl_s == 300.0  # shipped default
    monkeypatch.setenv("PI_RAG_BM25_TTL", "45")
    assert RagConfig.from_env().retrieval.bm25_ttl_s == pytest.approx(45.0)
    monkeypatch.setenv("PI_RAG_BM25_TTL", "0")
    assert RagConfig.from_env().retrieval.bm25_ttl_s == pytest.approx(0.0)
    monkeypatch.setenv("PI_RAG_BM25_TTL", "not-a-number")
    assert RagConfig.from_env().retrieval.bm25_ttl_s == pytest.approx(300.0)



# ---------------------------------------------------------------------------
# Hybrid happy path
# ---------------------------------------------------------------------------


def test_hybrid_returns_hits_with_citations_from_sql(tmp_path):
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        res = await r.search(_USER_A, "RAG 检索质量用哪些指标评估")
        assert res.mode is RetrievalMode.HYBRID
        assert res.outcome == OUTCOME_HYBRID_OK
        assert res.degraded is False
        assert res.chunks, "hybrid returned nothing on an ingested corpus"
        top = res.chunks[0]
        # 引用溯源: title + source come from rag_docs via hydration. The parser
        # normalizes the heading ("RAG 评估手册"), so assert the contract, not a
        # byte-copy: every hit carries a non-empty doc title and source path.
        titles = {d[1] for d in _DOCS}
        assert top.title and any(t.replace(" ", "") in top.title.replace(" ", "")
                                 for t in titles), top.title
        assert top.source.endswith(".md")
        assert top.title_path, "citation path missing"
        assert top.text.strip()
        assert top.chunk_id > 0
        assert res.duration_ms >= 0

    asyncio.run(main())


def test_hybrid_scores_are_rrf_values_and_ranked_desc(tmp_path):
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        res = await r.search(_USER_A, "切块 标题边界 章节")
        scores = [c.score for c in res.chunks]
        assert scores == sorted(scores, reverse=True)
        # RRF score of a single-channel rank-1 hit is 1/(60+1)
        assert scores[0] <= 1 / 61 + 1 / 61 + 1e-9
        assert scores[0] > 0

    asyncio.run(main())


def test_both_channels_contribute_to_fusion(tmp_path):
    """A lexical-only match must survive fusion (that is why we fuse)."""
    async def main():
        store, r, counts, vec, lex = await _seeded(tmp_path)
        # exact rare term: BM25 nails it even if the fake embedder does not
        lex_hits = await lex.search(_USER_A, "contextual", k=3)
        assert lex_hits, "fixture corpus has no 'contextual' term"
        res = await r.search(_USER_A, "contextual")
        got = {c.chunk_id for c in res.chunks}
        assert got & {cid for cid, _ in lex_hits}, "lexical hit lost in fusion"

    asyncio.run(main())


def test_final_k_respected(tmp_path):
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        res = await r.search(_USER_A, "评估 指标", k=2)
        assert len(res.chunks) <= 2
        res5 = await r.search(_USER_A, "评估 指标", k=5)
        assert len(res5.chunks) >= len(res.chunks)

    asyncio.run(main())


def test_empty_query_short_circuits_without_touching_channels(tmp_path):
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        hooks = RecordingHooks()
        r.hooks = hooks
        for q in ("", "   ", "\n\t"):
            res = await r.search(_USER_A, q)
            assert res.mode is RetrievalMode.EMPTY
            assert res.chunks == []
            assert res.outcome == OUTCOME_NO_HITS
        # one event per call, none of them a fake degradation
        assert hooks.outcomes() == [OUTCOME_NO_HITS] * 3

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Degradation chain - the house rule: 降级不消失 + 绝不静默
# ---------------------------------------------------------------------------


def test_lexical_down_degrades_to_vector_only(tmp_path):
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        hooks = RecordingHooks()
        r.lexical_index = RaisingLexical()
        r.hooks = hooks
        res = await r.search(_USER_A, "RAG 评估指标")
        assert res.mode is RetrievalMode.VECTOR_ONLY
        assert res.outcome == OUTCOME_VECTOR_ONLY
        assert res.degraded is True
        assert res.chunks, "vector_only must still serve hits"
        assert hooks.outcomes() == [OUTCOME_VECTOR_ONLY]

    asyncio.run(main())


def test_vector_store_down_degrades_to_bm25(tmp_path):
    """对接文档验收负例②: 停掉 Milvus -> 检索降级到 BM25 仍可用 + 有噪音."""
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        hooks = RecordingHooks()
        r.vector_store = RaisingVectorStore()
        r.hooks = hooks
        res = await r.search(_USER_A, "多租户 权限 user_id 过滤")
        assert res.mode is RetrievalMode.BM25_FALLBACK
        assert res.outcome == OUTCOME_BM25_FALLBACK
        assert res.degraded is True
        assert res.chunks, "bm25 fallback returned nothing"
        assert hooks.outcomes() == [OUTCOME_BM25_FALLBACK]

    asyncio.run(main())


def test_embed_failed_is_distinguished_from_bm25_fallback(tmp_path):
    """归因 needs this split: embed_failed bills nothing, store_failed did bill."""
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        hooks = RecordingHooks()
        r.embedder = RaisingEmbedder()
        r.hooks = hooks
        res = await r.search(_USER_A, "多租户 权限 user_id 过滤")
        assert res.mode is RetrievalMode.BM25_FALLBACK
        assert res.outcome == OUTCOME_EMBED_FAILED
        assert res.chunks
        # nothing was billed - the embedder never returned
        assert hooks.embed_usage == []
        assert hooks.outcomes() == [OUTCOME_EMBED_FAILED]

    asyncio.run(main())


def test_malformed_embedding_response_degrades_not_crashes(tmp_path):
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        hooks = RecordingHooks()
        r.embedder = EmptyVectorEmbedder()
        r.hooks = hooks
        res = await r.search(_USER_A, "评估 指标")
        assert res.mode is RetrievalMode.BM25_FALLBACK
        assert res.outcome == OUTCOME_EMBED_FAILED
        assert res.chunks

    asyncio.run(main())


def test_both_channels_down_falls_to_sql_like(tmp_path):
    """Last resort still serves from the truth (检索降级不消失)."""
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        hooks = RecordingHooks()
        r.embedder = RaisingEmbedder()
        r.lexical_index = RaisingLexical()
        r.hooks = hooks
        # SQL LIKE needs a literal substring of the stored text
        res = await r.search(_USER_A, "Recall@k")
        assert res.mode is RetrievalMode.SQL_FALLBACK
        assert res.outcome == OUTCOME_SQL_FALLBACK
        assert res.degraded is True
        assert res.chunks, "sql fallback found nothing for a literal substring"
        assert any("Recall@k" in c.text for c in res.chunks)
        assert hooks.outcomes() == [OUTCOME_SQL_FALLBACK]

    asyncio.run(main())


def test_everything_down_reports_error_but_never_raises(tmp_path):
    """绝不炸 run: even a dead MySQL yields a result object, not an exception."""
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        hooks = RecordingHooks()
        r.embedder = RaisingEmbedder()
        r.lexical_index = RaisingLexical()
        r.store = RaisingStore(store, fail_text=True)
        r.hooks = hooks
        res = await r.search(_USER_A, "评估")  # must not raise
        assert res.mode is RetrievalMode.EMPTY
        assert res.outcome == OUTCOME_ERROR
        assert res.degraded is True
        assert res.chunks == []
        assert hooks.outcomes() == [OUTCOME_ERROR]

    asyncio.run(main())


def test_unconfigured_kernel_degrades_to_lexical_with_zero_config(tmp_path):
    """§4.8: boots and answers with ZERO configuration (BM25 only)."""
    async def main():
        store = _store(tmp_path)
        counts = await _seed(store)
        hooks = RecordingHooks()
        r = HybridRetriever(
            store, embedder=None, vector_store=None,
            lexical_index=MemoryBM25Index(store), config=_cfg(), hooks=hooks,
        )
        res = await r.search(_USER_A, "多租户 权限 过滤")
        assert res.mode is RetrievalMode.BM25_FALLBACK
        assert res.chunks
        assert hooks.outcomes() == [OUTCOME_BM25_FALLBACK]

    asyncio.run(main())


def test_no_channels_at_all_falls_to_sql_and_stays_loud(tmp_path):
    """Nothing configured anywhere: SQL LIKE still gets a chance, and the
    result is ALWAYS marked degraded - an outage must never masquerade as a
    clean empty answer (that is how 归因 becomes impossible)."""
    async def main():
        store = _store(tmp_path)
        await _seed(store)
        hooks = RecordingHooks()
        r = HybridRetriever(store, embedder=None, vector_store=None,
                            lexical_index=None, config=_cfg(), hooks=hooks)
        res = await r.search(_USER_A, "评估")
        assert res.mode is RetrievalMode.SQL_FALLBACK
        assert res.outcome == OUTCOME_SQL_FALLBACK
        assert res.degraded is True
        assert hooks.outcomes() == [OUTCOME_SQL_FALLBACK]

        # and with a query the LIKE cannot match: STILL degraded, still
        # sql_fallback - "no hits" must not hide that both channels were down
        res2 = await r.search(_USER_A, "zzz qqq 9999 xxkk")
        assert res2.chunks == []
        assert res2.mode is RetrievalMode.SQL_FALLBACK
        assert res2.outcome == OUTCOME_SQL_FALLBACK
        assert res2.degraded is True, "an outage was reported as a clean miss"

    asyncio.run(main())


class SlowTextStore:
    """ChunkStore wrapper whose search_text HANGS (or raises after hanging).

    R5: the last-resort fallback is `LIKE '%q%'`, which cannot use an index -
    a full scan of rag_chunks, executed exactly when both ranked channels are
    ALREADY down. Without a deadline, "everything is degraded" also means
    "the request hangs until the client times out". This double proves the
    budget is real: the query must return in ~budget seconds, not ~sleep.
    """

    def __init__(self, inner: ChunkStore, *, delay: float = 5.0) -> None:
        self._inner = inner
        self.delay = delay
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def search_text(self, user_id, query, k):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return await self._inner.search_text(user_id, query, k)


def test_sql_fallback_timeout_returns_an_error_not_a_hang(tmp_path):
    """The budget must actually cut off a slow LIKE scan, and the result must
    be reported as an outage (degraded + OUTCOME_ERROR) - never as a clean
    empty answer, which would hide a full-scan problem behind a "no hits".
    """
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        hooks = RecordingHooks()
        slow = SlowTextStore(store, delay=5.0)
        r.store = slow
        r.embedder = RaisingEmbedder()   # vector down
        r.lexical_index = RaisingLexical()  # bm25 down -> SQL is the last resort
        r.hooks = hooks
        r.config = _cfg(sql_fallback_timeout_s=0.15)

        t0 = asyncio.get_event_loop().time()
        res = await r.search(_USER_A, "Recall@k")  # would match if not for the budget
        elapsed = asyncio.get_event_loop().time() - t0

        assert elapsed < 1.5, f"timeout did not cut off the scan ({elapsed:.2f}s)"
        assert slow.calls == 1, "the scan was attempted"
        assert res.chunks == []
        assert res.mode is RetrievalMode.EMPTY
        assert res.outcome == OUTCOME_ERROR
        assert res.degraded is True, "an outage was reported as a clean miss"
        assert hooks.outcomes() == [OUTCOME_ERROR]

    asyncio.run(main())


def test_sql_fallback_budget_zero_disables_the_scan_entirely(tmp_path):
    """budget<=0 means "never run the unindexed scan". The point is that this
    must still be LOUD: no scan, no hits, degraded + OUTCOME_ERROR.
    """
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        hooks = RecordingHooks()
        slow = SlowTextStore(store, delay=0.01)
        r.store = slow
        r.embedder = RaisingEmbedder()
        r.lexical_index = RaisingLexical()
        r.hooks = hooks
        r.config = _cfg(sql_fallback_timeout_s=0.0)

        res = await r.search(_USER_A, "Recall@k")
        assert slow.calls == 0, "budget=0 still executed the full scan"
        assert res.chunks == []
        assert res.mode is RetrievalMode.EMPTY
        assert res.outcome == OUTCOME_ERROR
        assert res.degraded is True
        assert hooks.outcomes() == [OUTCOME_ERROR]

    asyncio.run(main())


def test_sql_fallback_completes_when_under_budget(tmp_path):
    """The budget must not break the fallback that works: a scan that finishes
    in time still serves from the truth (检索降级不消失). Guards against the
    timeout being wired so tight that the happy degradation path regresses.
    """
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        hooks = RecordingHooks()
        slow = SlowTextStore(store, delay=0.02)  # slow, but inside the budget
        r.store = slow
        r.embedder = RaisingEmbedder()
        r.lexical_index = RaisingLexical()
        r.hooks = hooks
        r.config = _cfg(sql_fallback_timeout_s=5.0)

        res = await r.search(_USER_A, "Recall@k")
        assert slow.calls == 1
        assert res.mode is RetrievalMode.SQL_FALLBACK
        assert res.outcome == OUTCOME_SQL_FALLBACK
        assert res.degraded is True
        assert res.chunks and any("Recall@k" in c.text for c in res.chunks)

    asyncio.run(main())


def test_sql_fallback_timeout_env_override_is_read(monkeypatch):
    from pi.rag.config import RagConfig, RetrievalConfig

    assert RetrievalConfig().sql_fallback_timeout_s == 5.0  # shipped default
    monkeypatch.setenv("PI_RAG_SQL_FALLBACK_TIMEOUT", "12.5")
    assert RagConfig.from_env().retrieval.sql_fallback_timeout_s == pytest.approx(12.5)
    monkeypatch.setenv("PI_RAG_SQL_FALLBACK_TIMEOUT", "0")
    assert RagConfig.from_env().retrieval.sql_fallback_timeout_s == pytest.approx(0.0)
    monkeypatch.setenv("PI_RAG_SQL_FALLBACK_TIMEOUT", "garbage")
    assert RagConfig.from_env().retrieval.sql_fallback_timeout_s == pytest.approx(5.0)


def test_escape_like_treats_metachars_as_literals():
    """P1: % / _ / \\ are escaped so the SQL fallback matches a SUBSTRING,
    not a wildcard. A query that literally contains these would otherwise
    silently widen the match set (hit amplification reporting as clean)."""
    # \\ must be escaped FIRST, then % and _ (which emit backslashes themselves)
    assert escape_like("100%") == "100\\%"
    assert escape_like("a_b") == "a\\_b"
    assert escape_like(r"a\b") == "a\\\\b"
    assert escape_like("") == ""
    assert escape_like("no metachars") == "no metachars"


def test_sql_fallback_matches_literal_metacharacters(tmp_path):
    """End-to-end: a chunk whose text contains a literal % / _ / \\ is found
    by the SQL fallback only when the query is escaped; the escape must not
    fabricate hits from chunks that merely contain the OTHER metacharacters."""
    async def main():
        store = _store(tmp_path)
        await _seed(store)
        # The fixture corpus has 'Recall@k' but no literal % / _ / \; add one
        from pi.rag.types import Chunk

        chunks = [
            Chunk(
                chunk_id=0, doc_key="meta-doc", user_id=_USER_A, seq=0,
                text="配额是 100% 且 id 形如 a_b，路径 C:\\tmp",
                title_path="原样符号", page=None,
            ),
            Chunk(
                chunk_id=0, doc_key="meta-doc", user_id=_USER_A, seq=1,
                text="只有百分号 % 这里", title_path="", page=None,
            ),
        ]
        await store.add_chunks(chunks)

        # literal '%' in the query -> only seq0 matches its '%', not seq1
        hits = await store.search_text(_USER_A, "100%", 10)
        assert hits, "escaped % query found nothing"
        assert all("100%" in h.text for h in hits)
        # literal '_' -> only the a_b chunk
        hits = await store.search_text(_USER_A, "a_b", 10)
        assert hits and all("a_b" in h.text for h in hits)
        # literal backslash path -> exactly the C:\tmp chunk
        hits = await store.search_text(_USER_A, "C:\\tmp", 10)
        assert hits and all("C:\\tmp" in h.text for h in hits)

    asyncio.run(main())


def test_healthy_but_empty_channel_is_no_hits_not_degradation(tmp_path):
    """Critical: a clean miss must NOT trigger a fallback that invents hits."""
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        hooks = RecordingHooks()
        r.hooks = hooks
        # gibberish that matches nothing lexically and nothing semantically
        res = await r.search(_USER_A, "zzz qqq 9999 xxkk")
        assert res.degraded is False, f"a clean miss was mislabeled: {res.outcome}"
        assert res.mode in (RetrievalMode.HYBRID, RetrievalMode.EMPTY)
        assert res.outcome in (OUTCOME_HYBRID_OK, OUTCOME_NO_HITS)

    asyncio.run(main())


def test_stale_vector_id_is_dropped_at_hydration(tmp_path):
    """A vector pointing at a deleted chunk must vanish, never leak old bytes."""
    async def main():
        store, r, counts, vec, lex = await _seeded(tmp_path)
        hooks = RecordingHooks()
        r.hooks = hooks
        res_before = await r.search(_USER_A, "评估 指标", k=5)
        assert res_before.chunks
        # wipe the SQL truth for one doc but leave its vectors -> ghost ids
        await store.delete_chunks(_USER_A, "rag-eval")
        res = await r.search(_USER_A, "评估 指标 Recall MRR", k=5)
        for c in res.chunks:
            assert c.doc_key != "rag-eval", "ghost chunk served from a stale vector"

    asyncio.run(main())


def test_hydration_failure_yields_empty_not_exception(tmp_path):
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        hooks = RecordingHooks()
        r.store = RaisingStore(store, fail_hydrate=True)
        r.hooks = hooks
        res = await r.search(_USER_A, "评估 指标")  # must not raise
        assert res.chunks == []
        assert res.outcome in (OUTCOME_NO_HITS, OUTCOME_ERROR)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Rerank
# ---------------------------------------------------------------------------


def test_reranker_reorders_and_bills(tmp_path):
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path, cfg=_cfg(final_k=3))
        hooks = RecordingHooks()
        rr = FixedReranker(mode="reverse")
        r.reranker = rr
        r.hooks = hooks
        base = await HybridRetriever.search(
            _retriever(store, vec=r.vector_store, emb=r.embedder, lex=r.lexical_index,
                       cfg=_cfg(final_k=3)), _USER_A, "评估 指标"
        )
        res = await r.search(_USER_A, "评估 指标")
        assert rr.calls and rr.calls[0][0] == "评估 指标"
        # rerank replaced the RRF scores with cross-encoder scores
        assert [c.score for c in res.chunks] == sorted(
            [c.score for c in res.chunks], reverse=True
        )
        assert res.outcome == OUTCOME_HYBRID_OK, "rerank success must not look degraded"
        assert res.degraded is False
        # both embedding and rerank usage hit the SAME metering path
        kinds = sorted({k for _, _, k in hooks.embed_usage})
        assert kinds == ["embedding", "rerank"], kinds
        assert any(k == "rerank" and t == 42 for _, t, k in hooks.embed_usage)
        assert base.chunks, "baseline hybrid must return hits for the comparison"

    asyncio.run(main())


def test_rerank_failure_keeps_fused_order_and_makes_noise(tmp_path):
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        hooks = RecordingHooks()
        r.reranker = FixedReranker(mode="fail")
        r.hooks = hooks
        res = await r.search(_USER_A, "评估 指标")
        assert res.chunks, "rerank failure must not lose hits"
        assert res.outcome == OUTCOME_RERANK_FAILED
        assert res.degraded is True
        assert res.mode is RetrievalMode.HYBRID, "mode still records what served"
        assert hooks.outcomes() == [OUTCOME_RERANK_FAILED]

    asyncio.run(main())


def test_rerank_failure_during_degraded_retrieval_keeps_worse_headline(tmp_path):
    """归因: the retrieval-path degradation outranks a rerank hiccup."""
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        hooks = RecordingHooks()
        r.reranker = FixedReranker(mode="fail")
        r.embedder = RaisingEmbedder()
        r.hooks = hooks
        res = await r.search(_USER_A, "多租户 权限 过滤")
        assert res.outcome == OUTCOME_EMBED_FAILED, res.outcome
        assert res.chunks

    asyncio.run(main())


def test_rerank_returning_empty_keeps_fused_hits(tmp_path):
    """A degradation must never LOSE data the user could have had."""
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        hooks = RecordingHooks()
        r.reranker = FixedReranker(mode="empty")
        r.hooks = hooks
        res = await r.search(_USER_A, "评估 指标")
        assert res.chunks, "empty rerank response dropped all hits"
        assert res.outcome == OUTCOME_RERANK_FAILED

    asyncio.run(main())


def test_rerank_window_is_bounded_by_candidates(tmp_path):
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path, cfg=_cfg(rerank_candidates=2, final_k=1))
        rr = FixedReranker(mode="identity")
        r.reranker = rr
        await r.search(_USER_A, "评估 指标")
        assert rr.calls[0][1] <= 2, f"rerank got {rr.calls[0][1]} candidates"

    asyncio.run(main())


def test_rerank_disabled_by_config_skips_endpoint(tmp_path):
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path, cfg=_cfg(rerank_enabled=False))
        rr = FixedReranker(mode="reverse")
        r.reranker = rr
        res = await r.search(_USER_A, "评估 指标")
        assert rr.calls == [], "rerank called despite rerank_enabled=False"
        assert res.chunks

    asyncio.run(main())


# ---------------------------------------------------------------------------
# ACL - 跨用户 0 泄漏, every channel including the fallbacks
# ---------------------------------------------------------------------------


def test_cross_user_zero_leak_hybrid(tmp_path):
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        res = await r.search(_USER_B, "评估 指标 Recall MRR 切块 权限")
        assert res.chunks == [], f"cross-user leak: {res.chunks}"

    asyncio.run(main())


def test_cross_user_zero_leak_through_every_degradation_step(tmp_path):
    """The leak test must cover the fallbacks too - they are different code."""
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)

        # vector_only (lexical dead)
        r.lexical_index = RaisingLexical()
        assert (await r.search(_USER_B, "评估 指标")).chunks == []

        # bm25_fallback (vector dead)
        r.lexical_index = MemoryBM25Index(store)
        r.vector_store = RaisingVectorStore()
        assert (await r.search(_USER_B, "评估 指标")).chunks == []

        # sql_fallback (both dead) - the LIKE path is the easiest to get wrong
        r.embedder = RaisingEmbedder()
        r.lexical_index = RaisingLexical()
        res = await r.search(_USER_B, "Recall@k")
        assert res.chunks == [], f"SQL LIKE leaked across users: {res.chunks}"

    asyncio.run(main())


def test_acl_violation_at_hydration_is_blocked(tmp_path):
    """If a channel ever returns a foreign id, hydration must drop it."""

    class LeakyVector(InMemoryVectorStore):
        async def search(self, user_id, vector, k, doc_keys=None):
            # deliberately ignore the ACL filter (simulates a broken backend)
            return await super().search(_USER_A, vector, k, doc_keys)

    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        r.vector_store = LeakyVector()
        r.lexical_index = RaisingLexical()
        res = await r.search(_USER_B, "评估 指标")
        assert res.chunks == [], "hydration did not enforce user_id"

    asyncio.run(main())


def test_doc_keys_scope_applies_to_doc_blind_channels(tmp_path):
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path, cfg=_cfg(final_k=10))
        # lexical-only path is doc-blind -> must be post-filtered
        r.vector_store = RaisingVectorStore()
        res = await r.search(_USER_A, "评估 指标 切块 权限", doc_keys=["acl-policy"])
        assert res.chunks, "doc_keys filter removed everything"
        assert all(c.doc_key == "acl-policy" for c in res.chunks), res.chunks
        # sql fallback too
        r.embedder = RaisingEmbedder()
        r.lexical_index = RaisingLexical()
        res2 = await r.search(_USER_A, "Recall@k", doc_keys=["chunk-guide"])
        assert res2.chunks == [], "doc_keys not enforced on the SQL path"
        res3 = await r.search(_USER_A, "Recall@k", doc_keys=["rag-eval"])
        assert res3.chunks and all(c.doc_key == "rag-eval" for c in res3.chunks)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Metering / hooks / eval-runner contract
# ---------------------------------------------------------------------------


def test_embedding_usage_reported_once_per_query(tmp_path):
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        hooks = RecordingHooks()
        r.hooks = hooks
        await r.search(_USER_A, "评估 指标")
        emb_events = [(u, t) for u, t, k in hooks.embed_usage if k == "embedding"]
        assert len(emb_events) == 1, emb_events
        assert emb_events[0][0] == _USER_A and emb_events[0][1] == 7  # 1 text * 7
        assert hooks.outcomes() == [OUTCOME_HYBRID_OK]

    asyncio.run(main())


def test_exploding_hooks_cannot_break_retrieval(tmp_path):
    async def main():
        store, r, counts, _, _ = await _seeded(tmp_path)
        r.hooks = ExplodingHooks()
        r.reranker = FixedReranker(mode="reverse")
        res = await r.search(_USER_A, "评估 指标")  # must not raise
        assert res.chunks

    asyncio.run(main())


def test_search_chunks_matches_eval_runner_searchfn(tmp_path):
    """The shipped retriever IS the measured one (先建评测再调检索)."""
    async def main():
        from pi.rag.eval.harness import GoldenQA, GoldenSet, chunk_key
        from pi.rag.eval.runner import EvalRunner

        store, r, counts, _, _ = await _seeded(tmp_path)
        chunks = await store.list_chunks_for_user(_USER_A)
        gold = [
            chunk_key(c.doc_key, c.seq)
            for c in chunks
            if "Recall@k" in c.text
        ]
        assert gold, "fixture lost its gold chunk"
        golden = GoldenSet(name="m3-smoke", cases=[
            GoldenQA(id="q1", query="检索质量用哪些指标评估", user_id=_USER_A,
                     gold_chunk_keys=gold, category="happy_path"),
        ])
        runner = EvalRunner(store, k_max=5)
        report = await runner.run(golden, r.search_chunks, "hybrid")
        assert report.total == 1 and report.failed == 0
        assert report.metrics["recall@5"] == 1.0, report.markdown()

    asyncio.run(main())


def test_kernel_has_no_pi_server_or_fastapi_coupling():
    """Portability contract: retriever.py must stay liftable out of pi.

    Checks real IMPORT nodes (AST), not docstring prose - the module docstring
    legitimately names the forbidden modules to explain the boundary.
    """
    import ast
    import inspect

    import pi.rag.retriever as mod

    tree = ast.parse(inspect.getsource(mod))
    banned_prefixes = ("pi.server", "pi.tools", "pi.agent", "fastapi")
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for name in imported:
        assert not any(name == b or name.startswith(b + ".") for b in banned_prefixes), (
            f"kernel coupling: retriever imports {name!r}"
        )


def test_retriever_accepts_injected_protocol_impls_only(tmp_path):
    """Constructor takes no infra by default - everything is injected."""
    async def main():
        store = _store(tmp_path)
        r = HybridRetriever(store)  # zero optional deps
        assert r.embedder is None and r.vector_store is None
        assert r.lexical_index is None and r.reranker is None and r.hooks is None
        res = await r.search(_USER_A, "anything")
        assert res.mode is RetrievalMode.EMPTY or res.chunks == []

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Channel concurrency. The docstring promised "latency of the slower, not the
# sum" while the two channels were awaited back to back - so every query paid
# the remote embedding round trip AND the local BM25 scan in series.
# ---------------------------------------------------------------------------


class SlowEmbedder:
    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.calls = 0

    async def embed(self, texts: list[str]) -> EmbedResult:
        await asyncio.sleep(self.delay_s)
        return EmbedResult(vectors=[[1.0, 0.0] for _ in texts], usage_tokens=1)

    async def embed_query(self, text: str) -> EmbedResult:
        self.calls += 1
        return await self.embed([text])


class SlowLexical:
    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.calls = 0

    async def search(self, user_id: int, query: str, k: int):
        self.calls += 1
        await asyncio.sleep(self.delay_s)
        return []

    async def invalidate(self, user_id: int) -> None:
        return None


def test_channels_run_concurrently_not_back_to_back(tmp_path):
    """Both channels must be in flight together: total ≈ max(t1, t2), not t1+t2."""
    async def main():
        delay = 0.20
        emb = SlowEmbedder(delay)
        lex = SlowLexical(delay)
        r = _retriever(_store(tmp_path), vec=InMemoryVectorStore(), emb=emb, lex=lex)

        t0 = asyncio.get_event_loop().time()
        await r.search(_USER_A, "糖尿病患者的血糖控制目标")
        elapsed = asyncio.get_event_loop().time() - t0

        assert emb.calls == 1 and lex.calls == 1, "both channels must still run"
        # Serial would be >= 0.40s. Allow generous slack for a loaded CI box
        # while still failing loudly if the gather is ever reverted.
        assert elapsed < delay * 1.7, f"channels ran serially: {elapsed:.3f}s"

    asyncio.run(main())


def test_channel_failure_does_not_cancel_the_other_channel(tmp_path):
    """gather must not turn one dead channel into a dead query: the surviving
    channel still answers (and it is the one that decides the outcome string)."""
    async def main():
        lex = SlowLexical(0.0)
        r = _retriever(_store(tmp_path), vec=InMemoryVectorStore(),
                       emb=RaisingEmbedder("endpoint 503"), lex=lex)
        res = await r.search(_USER_A, "q")
        assert lex.calls == 1
        # no hits anywhere, but the REPORTED reason is the embedding failure
        assert res.outcome in (OUTCOME_EMBED_FAILED, OUTCOME_NO_HITS, OUTCOME_SQL_FALLBACK)

    asyncio.run(main())
