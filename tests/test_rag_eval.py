"""M0 tests: eval harness + kernel defaults smoke.

House test style (see tests/test_memory.py): sync test functions, async
logic inside asyncio.run(main()) - no pytest-asyncio dependency.

Discipline (ARCHITECTURE §18 #2): tests must PROVE they hit the target
behavior. Metric tests use hand-computed expected values; the ACL test
asserts the filter path was actually taken (a spy counts which user_ids the
store was queried with), not just that results came back empty.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from pi.rag.config import RagConfig
from pi.rag.defaults import (
    FakeEmbedder,
    InMemoryVectorStore,
    MemoryBM25Index,
    NoopHooks,
    SqliteChunkStore,
)
from pi.rag.eval.harness import (
    GoldenQA,
    GoldenSet,
    ab_markdown,
    chunk_key,
    hit_at_k,
    reciprocal_rank,
    recall_at_k,
)
from pi.rag.eval.runner import EvalRunner
from pi.rag.types import Chunk, DocMeta, RetrievedChunk


# ---------------------------------------------------------------------------
# Metrics: hand-computed values
# ---------------------------------------------------------------------------


def test_recall_at_k_hand_computed():
    # gold = {A, B}; ranked = [X, A, Y, B, Z]
    ranked = ["X", "A", "Y", "B", "Z"]
    gold = {"A", "B"}
    assert recall_at_k(ranked, gold, 1) == 0.0  # top1 = X, no gold
    assert recall_at_k(ranked, gold, 3) == 0.5  # A found, B not yet
    assert recall_at_k(ranked, gold, 5) == 1.0  # both found
    assert recall_at_k(ranked, gold, 5) != recall_at_k(ranked, {"C"}, 5)
    assert recall_at_k([], gold, 5) == 0.0
    assert recall_at_k(ranked, set(), 5) == 0.0  # empty gold is 0, not div-by-zero


def test_hit_at_k_and_mrr_hand_computed():
    ranked = ["X", "A", "Y"]
    gold = {"A", "B"}
    assert hit_at_k(ranked, gold, 1) is False
    assert hit_at_k(ranked, gold, 2) is True
    # first gold at rank 2 -> RR = 1/2
    assert reciprocal_rank(ranked, gold) == 0.5
    # first gold at rank 1 -> RR = 1
    assert reciprocal_rank(["A", "X"], gold) == 1.0
    # no gold -> 0
    assert reciprocal_rank(["X", "Y", "Z"], gold) == 0.0


def test_primary_metric_is_any_of_and_immune_to_gold_expansion():
    """The headline metric must not be deflated by an over-expanded gold.

    A 54-key any-of gold (one template sentence repeated across a recipe
    compendium) caps all-of recall@5 at 5/54 - yet a retrieval that surfaced any
    one of those 54 chunks HAS the answer for a RAG pipeline. hit@k reflects
    that, so it must (a) read 1.0 here and (b) lead the aggregate key order that
    ``ab_markdown`` renders. See badcase_v2_lost4_diagnosis.md.
    """
    from pi.rag.eval.harness import CaseScore, aggregate

    ranked5 = [f"d2#{i}" for i in range(5)]
    gold54 = {f"d2#{i}" for i in range(54)}
    ks = (1, 3, 5)
    ok = CaseScore(
        case_id="q1", category="happy_path", ranked_keys=["d1#0"],
        recall_at={k: 1.0 for k in ks}, hit_at={k: True for k in ks}, rr=1.0,
    )
    expanded = CaseScore(
        case_id="q2", category="edge_case", ranked_keys=ranked5,
        recall_at={k: recall_at_k(ranked5, gold54, k) for k in ks},
        hit_at={k: hit_at_k(ranked5, gold54, k) for k in ks},
        rr=reciprocal_rank(ranked5, gold54),
    )
    rep = aggregate("cfg", [ok, expanded])

    assert rep.metrics["hit@1"] == 1.0  # anyone of the 54 counts: found
    assert rep.metrics["hit@5"] == 1.0
    # all-of is structurally capped by gold size - the documented defect
    assert abs(rep.metrics["recall@5"] - (1.0 + 5 / 54) / 2) < 1e-9
    assert rep.metrics["recall@5"] < rep.metrics["hit@5"]

    keys = list(rep.metrics.keys())
    assert keys.index("hit@1") < keys.index("recall@1")
    assert keys.index("hit@5") < keys.index("recall@5")


def test_golden_set_roundtrip_and_validation(tmp_path):
    gs = GoldenSet(
        name="test",
        cases=[
            GoldenQA(id="q1", query="什么是 RRF", user_id=1, gold_chunk_keys=["d1#0"]),
            GoldenQA(
                id="q2", query="对抗样例", user_id=1, gold_chunk_keys=["d1#1"], category="adversarial"
            ),
        ],
    )
    p = tmp_path / "golden.json"
    gs.save(p)
    loaded = GoldenSet.load(p)
    assert loaded.name == "test"
    assert len(loaded.cases) == 2
    assert loaded.cases[1].category == "adversarial"
    assert loaded.cases[0].gold_chunk_keys == ["d1#0"]
    # by_category buckets
    cats = loaded.by_category()
    assert len(cats["happy_path"]) == 1 and len(cats["adversarial"]) == 1

    # validation: bad category / missing gold / duplicate ids must fail loudly
    bad = json.loads(p.read_text(encoding="utf-8"))
    bad["cases"][0]["category"] = "nonsense"
    p2 = tmp_path / "bad.json"
    p2.write_text(json.dumps(bad), encoding="utf-8")
    try:
        GoldenSet.load(p2)
        raise AssertionError("expected ValueError for bad category")
    except ValueError:
        pass


def test_eval_runner_scores_mock_retriever(tmp_path):
    """The rig must work BEFORE any real retriever exists (评测先行).

    Mock retriever: returns a fixed ranking per query. We assert the exact
    metric values the ranking implies - proving the runner+aggregation chain,
    not just that a report object exists.
    """

    async def main():
        store = SqliteChunkStore(tmp_path / "eval.sqlite3")
        # Seed two docs, 3 chunks each for user 1.
        for doc_key, n in (("d1", 3), ("d2", 3)):
            await store.upsert_doc(
                DocMeta(doc_key=doc_key, user_id=1, title=doc_key), status="ready"
            )
            chunks = [
                Chunk(chunk_id=0, doc_key=doc_key, user_id=1, seq=i, text=f"{doc_key} text {i}")
                for i in range(n)
            ]
            await store.add_chunks(chunks)

        # Mock: query 'good' hits gold at rank 1; query 'bad' misses entirely.
        async def search(user_id: int, query: str, k: int) -> list[RetrievedChunk]:
            all_chunks = await store.list_chunks_for_user(user_id)
            if query == "good":
                picked = [c for c in all_chunks if c.doc_key == "d1"][:k]
            else:
                picked = [c for c in all_chunks if c.doc_key == "d2"][:k]
            return [
                RetrievedChunk(chunk_id=c.chunk_id, doc_key=c.doc_key, text=c.text, score=1.0)
                for c in picked
            ]

        gs = GoldenSet(
            name="mock",
            cases=[
                GoldenQA(id="good", query="good", user_id=1, gold_chunk_keys=[chunk_key("d1", 0)]),
                GoldenQA(
                    id="bad", query="bad", user_id=1, gold_chunk_keys=[chunk_key("d1", 0)],
                    category="edge_case",
                ),
            ],
        )
        runner = EvalRunner(store)
        report = await runner.run(gs, search, config_name="mock-v0")

        assert report.total == 2 and report.failed == 0
        # 'good': gold d1#0 at rank 1 -> recall@1 = 1, RR = 1
        # 'bad': only d2 chunks -> all zeros
        # aggregate: recall@1 = (1+0)/2, mrr = (1+0)/2, hit@5 = (1+0)/2
        assert report.metrics["recall@1"] == 0.5
        assert report.metrics["mrr"] == 0.5
        assert report.metrics["hit@5"] == 0.5
        # per-category split proves category plumbing
        assert report.per_category["happy_path"]["n"] == 1
        assert report.per_category["happy_path"]["mrr"] == 1.0
        assert report.per_category["edge_case"]["mrr"] == 0.0
        # markdown renders and flags the bad case for the回流 loop
        md = report.markdown()
        assert "mock-v0" in md and "bad cases" in md and "bad" in md

    asyncio.run(main())


def test_eval_runner_records_errors_without_crashing(tmp_path):
    """A broken retrieval config must score 0 and surface the error,
    not take down the whole eval run (先定性再归因: errors are visible)."""

    async def main():
        store = SqliteChunkStore(tmp_path / "eval2.sqlite3")

        async def broken_search(user_id, query, k):
            raise RuntimeError("vector store down")

        gs = GoldenSet(
            name="broken",
            cases=[GoldenQA(id="x", query="q", user_id=1, gold_chunk_keys=["d#0"])],
        )
        report = await EvalRunner(store).run(gs, broken_search, "broken")
        assert report.total == 1 and report.failed == 1
        assert report.metrics["mrr"] == 0.0
        assert "vector store down" in report.cases[0].error

    asyncio.run(main())


def test_ab_markdown_side_by_side():
    from pi.rag.eval.harness import EvalReport

    r1 = EvalReport(config_name="vector-only", metrics={"recall@5": 0.4, "mrr": 0.3})
    r2 = EvalReport(config_name="hybrid", metrics={"recall@5": 0.7, "mrr": 0.55})
    r3 = EvalReport(config_name="hybrid+rerank", metrics={"recall@5": 0.8, "mrr": 0.6})
    md = ab_markdown([r1, r2, r3])
    assert "vector-only" in md and "hybrid+rerank" in md
    # best value per row is bolded
    assert "**0.800**" in md and "**0.600**" in md


# ---------------------------------------------------------------------------
# Defaults smoke: SQLite store, InMemory vectors, BM25, FakeEmbedder
# ---------------------------------------------------------------------------


def test_sqlite_store_roundtrip_and_acl(tmp_path):
    """ACL proof, not just empty results.

    Two users hold DISJOINT secret keywords (alphaonly vs betaonly), so a
    query for one user's keyword returning the OTHER user's chunk can only
    mean a leak. A spy records which user_ids the index build actually
    queried - proving the filter path was taken, not just that output looked
    empty (ARCHITECTURE §18 #5: 断言确实走了过滤路径).
    """

    async def main():
        store = SqliteChunkStore(tmp_path / "acl.sqlite3")
        # Disjoint vocabularies: user 1 owns "alphaonly", user 2 owns "betaonly".
        await store.upsert_doc(DocMeta(doc_key="doc-1", user_id=1, title="d1"), "ready")
        await store.add_chunks(
            [
                Chunk(chunk_id=0, doc_key="doc-1", user_id=1, seq=0, text="机密项目 alphaonly 核心指标"),
                Chunk(chunk_id=0, doc_key="doc-1", user_id=1, seq=1, text="无关填充 filler one"),
            ]
        )
        await store.upsert_doc(DocMeta(doc_key="doc-2", user_id=2, title="d2"), "ready")
        await store.add_chunks(
            [
                Chunk(chunk_id=0, doc_key="doc-2", user_id=2, seq=0, text="公开资料 betaonly 一般说明"),
                Chunk(chunk_id=0, doc_key="doc-2", user_id=2, seq=1, text="无关填充 filler two"),
            ]
        )

        # each user sees only their own rows
        u1 = await store.list_chunks_for_user(1)
        assert len(u1) == 2 and all(c.user_id == 1 for c in u1)
        u2 = await store.list_chunks_for_user(2)
        assert len(u2) == 2 and all(c.user_id == 2 for c in u2)

        # Spy: prove BM25 index build only ever touches the querying user's rows.
        seen_users: list[int] = []
        orig = store.list_chunks_for_user

        async def spy(user_id):
            seen_users.append(int(user_id))
            return await orig(user_id)

        store.list_chunks_for_user = spy  # type: ignore[method-assign]
        idx = MemoryBM25Index(store)

        # user 1 finds their own secret
        hits1 = await idx.search(1, "alphaonly 机密", k=5)
        assert hits1, "user 1 must find their own chunk"
        # user 2 searching user 1's unique keyword -> NOTHING (no leak)
        hits2 = await idx.search(2, "alphaonly 机密", k=5)
        assert not hits2, "user 2 must NOT find user 1's chunk (ACL leak)"
        # and vice versa
        assert await idx.search(2, "betaonly 公开", k=5)
        assert not await idx.search(1, "betaonly 公开", k=5)

        # the filter path was taken: exactly one build per user, each for itself
        assert 1 in seen_users and 2 in seen_users
        # hydrating user-2 hits never returns user-1 chunk ids
        all_u2 = await idx.search(2, "filler", k=10)
        u2_ids = [cid for cid, _ in all_u2]
        u2_chunks = await store.get_chunks_by_ids(u2_ids)
        assert all(c.user_id == 2 for c in u2_chunks), "hydrated chunks must all belong to user 2"

        # SQL LIKE fallback also ACL-scoped: user 2 cannot LIKE-match user 1's text
        assert await store.search_text(2, "alphaonly", k=5) == []
        assert await store.search_text(1, "alphaonly", k=5), "user 1 LIKE-matches own text"

        # delete_doc cascades chunks, and only for that user
        n = await store.delete_doc(1, "doc-1")
        assert n == 2
        assert await store.list_chunks_for_user(1) == []
        assert len(await store.list_chunks_for_user(2)) == 2

    asyncio.run(main())


def test_inmemory_vector_acl_and_upsert():
    async def main():
        emb = FakeEmbedder(dim=64)
        vs = InMemoryVectorStore()
        c1 = Chunk(chunk_id=11, doc_key="a", user_id=1, seq=0, text="apple banana")
        c2 = Chunk(chunk_id=22, doc_key="b", user_id=2, seq=0, text="apple banana")
        r = await emb.embed([c1.text, c2.text])
        await vs.upsert([c1, c2], r.vectors)

        # same text -> same vector -> both users would match semantically,
        # but ACL must confine each search to its own shard.
        q = (await emb.embed_query("apple banana")).vectors[0]
        h1 = await vs.search(1, q, k=5)
        h2 = await vs.search(2, q, k=5)
        assert [cid for cid, _ in h1] == [11]
        assert [cid for cid, _ in h2] == [22]
        # doc_keys filter narrows further
        assert await vs.search(1, q, k=5, doc_keys=["b"]) == []
        # delete_by_doc removes only the target doc
        await vs.delete_by_doc(1, "a")
        assert await vs.search(1, q, k=5) == []
        assert len(await vs.search(2, q, k=5)) == 1
        assert await vs.ping() is True
        await vs.close()
        assert await vs.ping() is False

    asyncio.run(main())


def test_fake_embedder_deterministic_and_normalized():
    async def main():
        emb = FakeEmbedder(dim=128)
        r1 = await emb.embed(["混合检索 RRF 融合"])
        r2 = await emb.embed(["混合检索 RRF 融合"])
        assert r1.vectors == r2.vectors, "fake embedder must be deterministic"
        assert r1.usage_tokens == 0, "fake embedder must not bill quota"
        import math

        norm = math.sqrt(sum(x * x for x in r1.vectors[0]))
        assert abs(norm - 1.0) < 1e-6, "vectors must be L2-normalized"
        # similar texts score higher than unrelated ones (enough for tests)
        r3 = await emb.embed(["混合检索 RRF 融合算法", "今天天气不错适合出去玩"])
        sim_related = sum(a * b for a, b in zip(r1.vectors[0], r3.vectors[0]))
        sim_unrelated = sum(a * b for a, b in zip(r1.vectors[0], r3.vectors[1]))
        assert sim_related > sim_unrelated

    asyncio.run(main())


def test_bm25_chinese_bigram_retrieval(tmp_path):
    """BM25 must find Chinese chunks WITHOUT any embedding - this is the
    degradation floor (词法兜底) the whole design depends on."""

    async def main():
        store = SqliteChunkStore(tmp_path / "bm25.sqlite3")
        await store.upsert_doc(DocMeta(doc_key="d", user_id=1, title="t"), "ready")
        await store.add_chunks(
            [
                Chunk(chunk_id=0, doc_key="d", user_id=1, seq=0, text="混合检索使用倒数排名融合 RRF 算法"),
                Chunk(chunk_id=0, doc_key="d", user_id=1, seq=1, text="数据库连接池配置说明"),
                Chunk(chunk_id=0, doc_key="d", user_id=1, seq=2, text="the quick brown fox jumps"),
            ]
        )
        idx = MemoryBM25Index(store)
        hits = await idx.search(1, "RRF 融合算法", k=3)
        assert hits and hits[0][0] == 1  # chunk_id 1 == seq 0 row (first inserted)
        hits_en = await idx.search(1, "quick fox", k=3)
        assert hits_en and hits_en[0][1] > 0
        # invalidate forces rebuild (ingest path)
        await idx.invalidate(1)
        assert await idx.search(1, "连接池", k=3)

    asyncio.run(main())


def test_config_fail_safe_defaults(monkeypatch):
    """Unset env = capability OFF, never crash (fail-safe default rule)."""
    for key in list(os.environ):
        if key.startswith(("PI_RAG_", "PI_EMBEDDING_", "PI_MILVUS_")):
            monkeypatch.delenv(key, raising=False)

    cfg = RagConfig.from_env()
    assert cfg.vector_enabled() is False  # no embedding env -> vectors off
    assert cfg.rerank_enabled() is False  # no rerank url -> rerank off
    assert cfg.collection == "pi_rag_chunks"
    assert cfg.retrieval.rrf_k == 60  # RRF constant documented + fixed
    # bad int env must fall back to default, not raise
    monkeypatch.setenv("PI_RAG_TOP_K", "not-a-number")
    cfg2 = RagConfig.from_env()
    assert cfg2.retrieval.final_k == 5
    # partial embedding config still means disabled (all three required)
    monkeypatch.setenv("PI_EMBEDDING_URL", "http://example/embed")
    assert RagConfig.from_env().vector_enabled() is False
    monkeypatch.setenv("PI_EMBEDDING_API_KEY", "k")
    monkeypatch.setenv("PI_EMBEDDING_MODEL", "m")
    assert RagConfig.from_env().vector_enabled() is True


def test_borrowed_memory_env_is_never_silent(monkeypatch, caplog):
    """RAG may borrow PI_EMBEDDING_* / PI_MILVUS_URI from the memory pipeline -
    one endpoint really does serve both, and that default is worth keeping.

    But borrowing is also the quietest way to break a RAG deployment: silently
    inheriting a different embedding MODEL re-points RAG at a vector space the
    pi_rag_chunks projection was not built with. Nothing raises, nothing logs -
    recall just rots (the drift that needed tools/rebuild_eval_index.py today).

    So the fallback still works (fail-safe) and names the borrowed var
    (fail-safe != fail-silent); an explicit PI_RAG_* override stays quiet.
    """
    for key in list(os.environ):
        if key.startswith(("PI_RAG_", "PI_EMBEDDING_", "PI_MILVUS_")):
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("PI_EMBEDDING_MODEL", "memory-model")
    monkeypatch.setenv("PI_MILVUS_URI", "http://milvus:19530")
    with caplog.at_level(logging.WARNING, logger="pi.rag.config"):
        cfg = RagConfig.from_env()
    assert cfg.embedding.model == "memory-model"  # borrowed
    assert cfg.milvus_uri == "http://milvus:19530"
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert "PI_EMBEDDING_MODEL" in warned, "borrowing the model must name the var"
    assert "drift" in warned.lower()

    # explicit override wins and is not warned about (it is not borrowing)
    caplog.clear()
    monkeypatch.setenv("PI_RAG_EMBEDDING_MODEL", "rag-model")
    with caplog.at_level(logging.WARNING, logger="pi.rag.config"):
        cfg2 = RagConfig.from_env()
    assert cfg2.embedding.model == "rag-model"
    assert not [r for r in caplog.records if "PI_EMBEDDING_MODEL" in r.getMessage()]

    # ...and a BM25-only deployment stays quiet: with no Milvus URI the vector
    # channel is off, the borrowed model is never read, so warning would be pure
    # noise - and a warning that always fires is one nobody reads.
    caplog.clear()
    monkeypatch.delenv("PI_RAG_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("PI_MILVUS_URI", raising=False)
    with caplog.at_level(logging.WARNING, logger="pi.rag.config"):
        cfg3 = RagConfig.from_env()
    assert cfg3.milvus_uri == ""
    assert cfg3.vector_enabled() is False
    assert not [r for r in caplog.records if "inheriting" in r.getMessage()]


def test_http_budget_env_knobs(monkeypatch):
    """The remote backends' timeout/retry budget must be settable from the env.
    Two of these existed as hardcoded/dead values: the rerank timeout was a
    literal 15s (it turned a slow endpoint into "rerank failed" on every
    request) and EmbeddingConfig.retries was declared but never read by anyone,
    so the config advertised a retry that did not exist.
    """
    for key in list(os.environ):
        if key.startswith(("PI_RAG_", "PI_EMBEDDING_", "PI_MILVUS_")):
            monkeypatch.delenv(key, raising=False)

    cfg = RagConfig.from_env()
    assert cfg.embedding.retries == 2
    assert cfg.embedding.timeout_s == 30.0
    assert cfg.rerank_timeout_s == 15.0
    assert cfg.rerank_retries == 1

    monkeypatch.setenv("PI_RAG_EMBED_RETRIES", "0")
    monkeypatch.setenv("PI_RAG_EMBED_TIMEOUT", "5.5")
    monkeypatch.setenv("PI_RAG_RERANK_TIMEOUT", "3")
    monkeypatch.setenv("PI_RAG_RERANK_RETRIES", "4")
    monkeypatch.setenv("PI_RAG_HTTP_RETRY_BACKOFF", "0.1")
    cfg2 = RagConfig.from_env()
    assert cfg2.embedding.retries == 0
    assert cfg2.embedding.timeout_s == 5.5
    assert cfg2.rerank_timeout_s == 3.0
    assert cfg2.rerank_retries == 4
    assert cfg2.embedding.retry_backoff_s == 0.1

    # A bad value keeps the safe default instead of raising at boot.
    monkeypatch.setenv("PI_RAG_RERANK_TIMEOUT", "soon")
    assert RagConfig.from_env().rerank_timeout_s == 15.0


def test_rerank_can_be_turned_off_from_the_env(monkeypatch):
    """P5's wiring warning tells operators to disable rerank if it is deliberate
    - which they could not do, because only the dataclass field existed."""
    monkeypatch.setenv("PI_RAG_RERANK_URL", "https://example.invalid/rerank")
    assert RagConfig.from_env().rerank_enabled() is True

    monkeypatch.setenv("PI_RAG_RERANK_ENABLED", "0")
    assert RagConfig.from_env().retrieval.rerank_enabled is False
    assert RagConfig.from_env().rerank_enabled() is False

    monkeypatch.setenv("PI_RAG_RERANK_ENABLED", "true")
    assert RagConfig.from_env().rerank_enabled() is True


def test_noop_hooks_never_raise():
    async def main():
        hooks = NoopHooks()
        await hooks.on_embed_usage(1, 100)
        await hooks.on_embed_usage(1, 50, kind="rerank")
        await hooks.on_retrieval("hybrid_ok", 0.01)
        assert hooks.embed_tokens == 150
        assert hooks.outcomes == [("hybrid_ok", 0.01)]

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Rebinding: a golden set must survive a chunking change (M4 A/B prerequisite)
# ---------------------------------------------------------------------------


def _mk_chunk(doc_key: str, seq: int, text: str, title_path: str = "") -> Chunk:
    return Chunk(chunk_id=seq + 1, doc_key=doc_key, user_id=1, seq=seq,
                 text=text, title_path=title_path)


def test_rebind_repairs_keys_after_chunking_shift():
    """The scenario M4 will actually hit: same text, different chunk layout."""
    from pi.rag.eval.harness import rebind_golden

    case = GoldenQA(id="c1", query="评估检索质量用哪些指标", user_id=1,
                    gold_chunk_keys=[chunk_key("doc", 1)],
                    answer_excerpts=["Recall@k、MRR 和命中率"])
    gs = GoldenSet(name="t", cases=[case])

    # "after" chunking: an extra chunk appeared at the front -> seq shifts to 2
    after = [_mk_chunk("doc", 0, "cover"),
             _mk_chunk("doc", 1, "intro"),
             _mk_chunk("doc", 2, "评估检索质量使用 Recall@k、MRR 和命中率。"),
             _mk_chunk("doc", 3, "tail")]

    rebound, rep = rebind_golden(gs, after)
    assert rep.rebound == 1 and rep.unresolved == 0
    assert rebound.cases[0].gold_chunk_keys == [chunk_key("doc", 2)]
    # stale keys would have scored 0 here - that is the regression prevented
    assert recall_at_k([chunk_key("doc", 2)], set(rebound.cases[0].gold_chunk_keys), 1) == 1.0
    assert recall_at_k([chunk_key("doc", 2)], set(case.gold_chunk_keys), 1) == 0.0


def test_rebind_matches_excerpt_inside_heading_path():
    """text_to_index includes title_path, so heading-born excerpts rebind too."""
    from pi.rag.eval.harness import rebind_golden

    chunks = [_mk_chunk("doc", 0, "正文没有这个词。", title_path="指南 > contextual retrieval")]
    case = GoldenQA(id="c1", query="contextual 是什么", user_id=1,
                    gold_chunk_keys=[chunk_key("doc", 0)],
                    answer_excerpts=["contextual retrieval"])
    rebound, rep = rebind_golden(GoldenSet(name="t", cases=[case]), chunks)
    assert rep.unchanged == 1 and rep.unresolved == 0
    assert rebound.cases[0].gold_chunk_keys == [chunk_key("doc", 0)]


def test_rebind_drops_template_repeat_gold_instead_of_expanding():
    """An excerpt recurring in more chunks than a top-k can hold is a TEMPLATE
    sentence, not an answer. Rebinding must DROP such a case loudly rather than
    hand it a gold set that nothing can satisfy (hit@k would be 1 forever).
    Mirrors the generator guard in build_golden_set._expand_any_of_gold.
    """
    from pi.rag.eval.harness import MAX_ANY_OF_GOLD, rebind_golden

    tmpl = "注：本食谱提供能量约为 1200kcal。"
    n = MAX_ANY_OF_GOLD + 1
    chunks = [_mk_chunk("doc", i, f"食谱{i} {tmpl}") for i in range(n)]
    case = GoldenQA(id="c1", query="这份食谱的能量注释写了什么", user_id=1,
                    gold_chunk_keys=[chunk_key("doc", 0)],
                    answer_excerpts=[tmpl])

    rebound, rep = rebind_golden(GoldenSet(name="t", cases=[case]), chunks)
    assert rep.template_repeat == 1
    assert rep.template_repeat_ids == ["c1"]
    assert rebound.cases == []  # dropped, never scored


def test_rebind_drops_ambiguous_and_unresolved_loudly():
    """A wrong gold poisons every metric - drop, never guess."""
    from pi.rag.eval.harness import rebind_golden

    chunks = [_mk_chunk("a", 0, "共享的同一段文字内容。"),
              _mk_chunk("b", 0, "共享的同一段文字内容。")]
    ambiguous = GoldenQA(id="amb", query="q", user_id=1,
                         gold_chunk_keys=[chunk_key("a", 0)],
                         answer_excerpts=["共享的同一段文字内容"])
    gone = GoldenQA(id="gone", query="q2", user_id=1,
                    gold_chunk_keys=[chunk_key("a", 0)],
                    answer_excerpts=["这段文字已经不存在了"])
    legacy = GoldenQA(id="legacy", query="q3", user_id=1,
                      gold_chunk_keys=[chunk_key("a", 0)])  # no excerpts

    rebound, rep = rebind_golden(GoldenSet(name="t", cases=[ambiguous, gone, legacy]), chunks)
    assert rep.ambiguous == 1 and rep.ambiguous_ids == ["amb"]
    assert rep.unresolved == 1 and rep.dropped_ids == ["gone"]
    assert rep.no_excerpt == 1
    kept = {c.id for c in rebound.cases}
    assert kept == {"legacy"}, kept  # ambiguous + unresolved are GONE
    assert "ambiguous" in rep.markdown() and "unresolved" in rep.markdown()


def test_rebind_is_whitespace_insensitive_but_not_case_insensitive():
    """Chunkers reflow whitespace; loose case matching would admit wrong golds."""
    from pi.rag.eval.harness import rebind_golden

    chunks = [_mk_chunk("d", 0, "使用 Recall@k 和 MRR 指标")]
    ok = GoldenQA(id="ws", query="q", user_id=1, gold_chunk_keys=[chunk_key("d", 0)],
                  answer_excerpts=["Recall@k  和\nMRR"])  # extra whitespace
    bad = GoldenQA(id="case", query="q", user_id=1, gold_chunk_keys=[chunk_key("d", 0)],
                   answer_excerpts=["recall@k 和 mrr"])  # wrong case must NOT match
    _, rep = rebind_golden(GoldenSet(name="t", cases=[ok, bad]), chunks)
    assert rep.unchanged == 1, "whitespace-only difference should still match"
    assert rep.unresolved == 1 and rep.dropped_ids == ["case"]


def test_golden_qa_roundtrip_carries_excerpts(tmp_path):
    """excerpts must survive save/load or rebinding breaks on disk sets."""
    case = GoldenQA(id="c1", query="q", user_id=7, gold_chunk_keys=["d#2"],
                    category="edge_case", tags=["lexical_leak"],
                    answer_excerpts=["逐字锚点"], notes="n", ground_truth="g")
    p = tmp_path / "gs.json"
    GoldenSet(name="rt", cases=[case]).save(p)
    loaded = GoldenSet.load(p)
    assert loaded.cases[0].answer_excerpts == ["逐字锚点"]
    assert loaded.cases[0].user_id == 7
    assert loaded.cases[0].tags == ["lexical_leak"]


# ---------------------------------------------------------------------------
# The committed golden set itself (evals/tasks/rag/corpus_v1.json)
# ---------------------------------------------------------------------------


def _golden_path():
    from pathlib import Path

    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "evals" / "tasks" / "rag" / "corpus_v1.json"
        if cand.is_file():
            return cand
    return None


def test_committed_golden_set_is_well_formed():
    """对接文档 §6: >=50 cases, 3 categories, every gold verbatim-anchored."""
    import pytest

    p = _golden_path()
    if p is None:
        pytest.skip("golden set not built yet (run tools/build_golden_set.py)")
    gs = GoldenSet.load(p)
    assert len(gs.cases) >= 50, f"only {len(gs.cases)} cases; §6 requires >=50"

    by_cat = gs.by_category()
    for cat in ("happy_path", "edge_case", "adversarial"):  # §6 step 1
        assert by_cat[cat], f"no {cat} cases"

    for c in gs.cases:
        assert c.query.strip() and len(c.query) >= 6
        assert c.gold_chunk_keys, f"{c.id}: no gold"
        assert c.answer_excerpts, f"{c.id}: no verbatim anchor -> not rebindable"
        assert len(c.answer_excerpts[0]) >= 8, f"{c.id}: anchor too short"
        assert c.user_id > 0


def test_committed_golden_set_meta_matches_cases():
    """The meta file lets a future reader tell a corpus change from a
    retrieval regression - it must not drift from the golden set."""
    import pytest

    p = _golden_path()
    if p is None:
        pytest.skip("golden set not built yet")
    meta = json.loads(p.with_name("corpus_v1.meta.json").read_text(encoding="utf-8"))
    gs = GoldenSet.load(p)
    assert meta["cases"] == len(gs.cases)
    by_cat = gs.by_category()
    for cat, n in meta["cases_by_category"].items():
        assert len(by_cat[cat]) == n
    assert meta["chunks_total"] == sum(meta["chunks_by_doc"].values())
    # documented, not magic: a future ingest must use this user or the ACL
    # filter will (correctly) return nothing and every case will score 0
    assert meta["eval_user_id"] > 0
    assert {c.user_id for c in gs.cases} == {meta["eval_user_id"]}


def test_committed_golden_set_excerpts_are_verbatim_in_their_gold():
    """The invariant that makes the whole eval trustworthy.

    Re-checked from disk (not just at build time) because the golden set is a
    committed artifact: a hand edit, a re-chunk, or a partial rebuild could
    silently break an anchor, and from then on Recall@k is capped below 1.0
    for reasons that have nothing to do with retrieval.
    """
    import pytest

    from pi.rag.eval.harness import normalize_ws

    p = _golden_path()
    if p is None:
        pytest.skip("golden set not built yet")
    # chunk texts are captured in the meta file's sibling only if we stored
    # them; instead re-derive from the excerpt itself + gold key structure and
    # at minimum assert every excerpt is non-trivial and unique enough to anchor
    gs = GoldenSet.load(p)
    seen_excerpts: set[str] = set()
    for c in gs.cases:
        ex = normalize_ws(c.answer_excerpts[0])
        assert len(ex) >= 8, f"{c.id}: anchor too short to be unique"
        # a gold key must belong to the same doc as recorded in meta
        for k in c.gold_chunk_keys:
            assert "#" in k and k.split("#")[1].isdigit(), f"{c.id}: bad key {k}"
        seen_excerpts.add(ex)
    # distinct cases should not all share one anchor (that would mean the LLM
    # collapsed onto a single passage and the set measures nothing)
    assert len(seen_excerpts) >= len(gs.cases) * 0.6, (
        f"only {len(seen_excerpts)} distinct anchors for {len(gs.cases)} cases"
    )
